ui = true
disable_mlock = true

# Keep this address aligned with Agentgateway's issuer configuration. The JWT
# issuer is <api_addr>/v1/identity/oidc; Vault also uses
# <api_addr>/v1/identity/oidc/provider/<name> for each configured OIDC provider.
api_addr = "http://localhost:8200"
