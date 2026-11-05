#!/bin/sh
set -eu

: "${GITHUB_ORG:?Set GITHUB_ORG in .env}"
: "${GITHUB_USER:?Set GITHUB_USER in .env}"
: "${DEX_VAULT_CLIENT_SECRET:?Set DEX_VAULT_CLIENT_SECRET in .env}"
if [ "$GITHUB_ORG" = your-github-org ] || [ "$GITHUB_USER" = your-github-username ]; then
  echo 'Edit GITHUB_ORG and GITHUB_USER in .env before starting the demo.' >&2
  exit 1
fi

for name in "$GITHUB_ORG" "$GITHUB_USER"; do
  case "$name" in
    *[!A-Za-z0-9-]*) echo 'GitHub org/user must contain only letters, digits and hyphens.' >&2; exit 1 ;;
  esac
done

# Team slugs are configuration, not asserted membership; Dex verifies membership.
case "${GITHUB_TEAMS:-}" in
  ,*|*,|*,,*|*[!A-Za-z0-9,-]*)
    echo 'GITHUB_TEAMS must be comma-separated GitHub team slugs.' >&2; exit 1 ;;
esac

vault policy write agentgateway /demo/vault-agentgateway-policy.hcl
# Authenticated demo users can generate signed JWTs from their already-bound
# Vault identity; they receive no configuration or entity-metadata write grant.
vault policy write agentgateway-login - <<'EOF'
path "identity/oidc/token/agentgateway-access" {
  capabilities = ["read"]
}
EOF

# Safe to rerun while the dev Vault is still running.
if ! vault auth list -format=json | grep -q '"oidc/"'; then
  vault auth enable -path=oidc oidc
fi
# Dex starts alongside this setup job. Discovery must work before configuring OIDC.
attempt=0
until vault write auth/oidc/config \
  oidc_discovery_url=http://localhost:5556/dex \
  oidc_client_id=vault \
  oidc_client_secret="$DEX_VAULT_CLIENT_SECRET" \
  default_role=agentgateway; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 30 ]; then
    echo 'Could not configure Vault OIDC; check docker compose logs dex.' >&2
    exit 1
  fi
  sleep 1
done

# Dex verifies organization membership; Vault independently restricts the username.
vault write auth/oidc/role/agentgateway - <<EOF
{
  "role_type": "oidc",
  "user_claim": "sub",
  "bound_audiences": ["vault"],
  "bound_claims": {"preferred_username": "$GITHUB_USER"},
  "claim_mappings": {"preferred_username": "username"},
  "oidc_scopes": ["profile", "email", "groups"],
  "groups_claim": "groups",
  "allowed_redirect_uris": [
    "http://localhost:8250/oidc/callback",
    "http://localhost:8200/ui/vault/auth/oidc/oidc/callback"
  ],
  "token_policies": ["agentgateway-login"],
  "token_ttl": "1h",
  "token_max_ttl": "1h",
  "token_no_default_policy": true
}
EOF

# Vault only resolves upstream groups with a pre-provisioned external group alias.
# Groups are embedded in Vault JWTs and used by A2A route authorization.
oidc_accessor=$(vault read -field=accessor sys/auth/oidc)
saved_ifs=$IFS
IFS=,
for team in ${GITHUB_TEAMS:-}; do
  group_name="$GITHUB_ORG:$team"
  if ! group_id=$(vault read -field=id "identity/group/name/$group_name" 2>/dev/null); then
    group_id=$(vault write -field=id identity/group name="$group_name" type=external)
  fi
  group_type=$(vault read -field=type "identity/group/id/$group_id")
  if [ "$group_type" != external ]; then
    echo "Expected external Vault group: $group_name" >&2; exit 1
  fi
  alias_id=$(vault read -field=alias -format=json "identity/group/id/$group_id" |
    sed -n 's/.*"id": "\([^"]*\)".*/\1/p')
  alias_path=identity/group-alias
  if [ -n "$alias_id" ]; then
    alias_path="$alias_path/id/$alias_id"
  fi
  vault write "$alias_path" name="$group_name" \
    mount_accessor="$oidc_accessor" canonical_id="$group_id" >/dev/null
done
IFS=$saved_ifs

# Direct identity tokens use the identity OIDC issuer and keys endpoint.
# Keep the access claim in lockstep with Agentgateway CEL allow rules.
vault write identity/oidc/key/agentgateway \
  algorithm=RS256 allowed_client_ids=agentgateway
oidc_jwt_template=$(sed "s/OIDC_MOUNT_ACCESSOR/$oidc_accessor/g" \
  /demo/vault-jwt-template.json)
vault write identity/oidc/role/agentgateway-access \
  key=agentgateway client_id=agentgateway ttl=1h \
  template="$oidc_jwt_template"

echo 'Vault browser OIDC auth and agentgateway JWT issuance configured.'
