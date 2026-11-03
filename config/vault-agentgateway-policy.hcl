# This entity metadata is operator-controlled: users who can only log in/mint the
# gateway audience's token cannot edit access grants or signing/provider config.
path "identity/oidc/token/agentgateway-access" {
  capabilities = ["read"]
}
