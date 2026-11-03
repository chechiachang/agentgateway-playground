#!/bin/sh
# Create operator-owned example entities with isolated access metadata.
# These are templates: operators map intended users to their entity IDs.
set -eu
: "${VAULT_ADDR:=http://localhost:8200}"
export VAULT_ADDR
: "${VAULT_TOKEN:?Use the local Vault root token from your private .env; never commit it}"

create_identity() {
  name=$1 access=$2
  id=$(vault write -field=id identity/entity name="$name" metadata=agentgateway_access="$access")
  printf '%-32s %s\n' "$name" "$id"
}


# Vault OIDC identity tokens accept one scalar `access` claim. Distinct values
# are intentionally mutually exclusive so no sample identity is overprivileged.
for spec in \
  'tools:calculate' \
  'tools:text-stats' \
  'tools:reverse-text' \
  'agents:calculator' \
  'agents:text-analyst' \
  'agents:text-transformer' \
  'agents:research' \
  'agents:data-analyst' \
  'agents:issue-triage' \
  'none'; do
  name=$(printf '%s' "$spec" | tr ':' '-')
  create_identity "demo-$name" "$spec"
done

printf '%s\n' 'Entities registered. Use Vault identity/entity-alias or identity/entity-alias/id/<alias-id> to associate an authenticated OIDC identity with a specific entity; entity metadata is operator-controlled.'
