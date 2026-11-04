.PHONY: setup login jwt codex coder coder-host model-catalog-refresh

CODEX_MODEL ?= gpt-6-luna

# Start the containerized A2A coding agent and apply its gateway route/secrets.
coder:
	@python3 scripts/setup-env.py
	@docker compose --profile coder build codex-agent
	@docker compose --profile coder up -d --no-build codex-agent

# Host alternative: set CODEX_AGENT_BACKEND=host.docker.internal:8082 in .env first.
coder-host:
	@python3 scripts/run-codex-agent.py

setup:
	@python3 scripts/setup-env.py

# Login to Vault through GitHub, then mint a scoped JWT: eval "$(make login)".
login:
	@python3 scripts/login.py
	@$(MAKE) --no-print-directory jwt

# Reuse the stored Vault session: eval "$(make jwt)".
jwt:
	@token=$$(docker compose exec -T vault vault read -field=token identity/oidc/token/agentgateway-access) || exit $$?; \
	  printf '%s' "$$token" | LC_ALL=C grep -Eq '^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$$' || { echo 'Vault returned an invalid JWT' >&2; exit 1; }; \
	  printf "export JWT='%s'\n" "$$token"

# Launch Codex through Agentgateway using the cached Vault session.
codex:
	@jwt_env=$$($(MAKE) --no-print-directory jwt) || { echo 'Run make login, then retry make codex.' >&2; exit 1; }; \
	  eval "$$jwt_env"; \
	  codex -m "$(CODEX_MODEL)" \
	    -c 'model_provider="agw"' \
	    -c 'model_providers.agw={name="Agentgateway",base_url="http://localhost:4000/v1",env_key="JWT",wire_api="responses",requires_openai_auth=false}'

# Refresh public model prices used by Agentgateway's local cost accounting.
model-catalog-refresh:
	@./scripts/refresh-model-catalog.sh
