# Repository Guidelines

- **Layout:** `examples/` contains MCP/A2A services; `config/` and `compose.yaml` configure Agentgateway and dependencies; `scripts/` has setup/check helpers; `tests/` uses Python `unittest`.
- **Run:** `make setup`; `docker compose up -d --build`; tests: `python3 -B -m unittest discover -s tests -v`.
- **MCP:** Use Agentgateway `/mcp` and discover permitted tools with `tools/list`, then call with `tools/call`. Tools include `playground_calculate`, `playground_text_stats`, `playground_reverse_text`, and `postgres_postgres_*` (read-only). Preserve JWT scopes and `nationalteam:sre` access policy.
- **A2A:** Cards: `/agents/{calculator,text-analyst,text-transformer,research,data-analyst,issue-triage,coder}/.well-known/agent-card.json`; `coder` requires its Compose profile. Delegate via `message/send` at `/agents/<name>`. Routes require an authorized `nationalteam:sre` caller. Caller credentials differ from agent API keys used for AGW model-provider authentication.
- **Style/security:** Follow nearby Python/YAML conventions. Keep `.env`, JWTs, API keys, and Vault tokens private; never commit secrets.
