# Agentgateway feature walkthrough

This guide demonstrates the local Compose stack: GitHub-backed sign-in, the UI and model APIs, least-privilege MCP tools, read-only PostgreSQL access, and six deterministic A2A agents. Keep this demo bound to localhost; it uses a development Vault and has no production rate limits.

## 1. Configure and start the stack

Requirements: Docker Compose, Python 3, `curl`, a GitHub OAuth App, and (for inference) Azure OpenAI credentials.

```sh
make setup
```

Edit `.env` and set `GITHUB_ORG`, `GITHUB_USER`, `GITHUB_OAUTH_CLIENT_ID`, `GITHUB_OAUTH_CLIENT_SECRET`, `AZURE_OPENAI_BASE_URL`, and `AZURE_OPENAI_API_KEY`. The OAuth callback must be `http://localhost:5556/dex/callback`; authorize the OAuth App for the configured organization. `make setup` generates local secrets and creates missing config files without replacing existing configs.

```sh
docker compose up -d --build --force-recreate
docker compose ps
```

Wait until Vault, PostgreSQL, `postgres-mcp`, and `demo-tools-agents` are healthy. `vault-init` should exit successfully; Agentgateway and Dex should be running. PostgreSQL is private to the Compose network, and its MCP reader has SELECT-only access.

## 2. Sign in and check the UI

In a terminal, start the GitHub OIDC login and keep the resulting JWT in the current shell:

```sh
eval "$(make login)"
```

Open <http://localhost:4000/ui>, choose **Sign in with SSO**, and sign in with the configured GitHub account. The gateway checks organization membership and the configured username. Visit **Models** at <http://localhost:4000/ui/llm/models> to see `gpt-6-luna`, `gpt-6.1-sol`, `text-embedding-3-large`, and `decision-1`.

## 3. Exercise authenticated model routes

With `JWT` set by `make login`, run the auth and inference smoke checks:

```sh
sh scripts/check-auth.sh
sh scripts/check-models.sh
```

The model check lists the catalog, calls chat completions for `gpt-6-luna` and `gpt-6.1-sol`, sends a native SystemOne request for `decision-1`, and requests an embedding. Inference calls use Azure quota. To provide a different SystemOne payload, pass a JSON file whose `model` is `decision-1`:

```sh
sh scripts/check-models.sh path/to/systemone-request.json
```

## 4. Demonstrate MCP tools

MCP is exposed at `http://localhost:4000/mcp`. It supports `tools/list` and `tools/call`; tool names are prefixed by their target. Use an identity granted only `tools:calculate` to list and call the calculator:

```sh
curl -sS http://localhost:4000/mcp \
  -H "Authorization: Bearer $JWT_TOOL_CALCULATE" \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'

curl -sS http://localhost:4000/mcp \
  -H "Authorization: Bearer $JWT_TOOL_CALCULATE" \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"playground_calculate","arguments":{"expression":"(12 + 8) * 3"}}}'
```

The other deterministic tools are `playground_text_stats` (try `Hello from agentgateway`) and `playground_reverse_text` (try `agentgateway`). Each requires its own `tools:text-stats` or `tools:reverse-text` identity; a token for one tool cannot call another.

## 5. Demonstrate read-only PostgreSQL MCP

Use a separate identity with `tools:postgres-read`. Discover accessible tables with `postgres_postgres_list_tables`, then call the query tool:

```sh
curl -sS http://localhost:4000/mcp \
  -H "Authorization: Bearer $JWT_TOOL_POSTGRES" \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"postgres_postgres_list_tables","arguments":{}}}'

curl -sS http://localhost:4000/mcp \
  -H "Authorization: Bearer $JWT_TOOL_POSTGRES" \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"postgres_postgres_query","arguments":{"sql":"SELECT 1 AS value"}}}'
```

The demo database starts without sample tables. Add data using a trusted database owner, then use `postgres_postgres_describe_table` with a table returned by `postgres_postgres_list_tables`. The MCP reader can only SELECT; queries are single-statement, bounded to 200 rows, and subject to database-side read-only and timeout limits. Do not use admin credentials for MCP.

## 6. Call A2A agents

Agent cards are available at `/agents/<name>/.well-known/agent-card.json`; messages use A2A JSON-RPC `message/send`. For example, with an identity granted only `agents:calculator`:

```sh
curl -sS http://localhost:4000/agents/calculator \
  -H "Authorization: Bearer $JWT_AGENT_CALCULATOR" \
  -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","id":1,"method":"message/send","params":{"message":{"kind":"message","role":"user","messageId":"demo-1","parts":[{"kind":"text","text":"(12 + 8) * 3"}]}}}'
```

Try these inputs with the matching, individually granted agent identity:

| Agent | Example input | Behavior |
|---|---|---|
| `calculator` | `(12 + 8) * 3` | Bounded arithmetic |
| `text-analyst` | `Hello from agentgateway` | Character, word, and line counts |
| `text-transformer` | `agentgateway` | Reverses Unicode text |
| `research` | `https://www.iana.org/domains/reserved` | Fetches a small public page from the HTTPS allowlist |
| `data-analyst` | `{"format":"csv","data":"team,score\nplatform,7\nsecurity,9\n"}` | Summarizes CSV or JSON records |
| `issue-triage` | `Security vulnerability with leaked credential` | Suggests a team using keywords |

Research makes an outbound request and only permits `www.iana.org`, `www.rfc-editor.org`, and `www.python.org`; it does not follow redirects. Data analysis has bounded input/row/column sizes. Issue triage is heuristic guidance only, not authorization or an action.

## 7. Configure route identities and run the access checks

The `JWT` from the normal GitHub login is suitable for model smoke checks, but does not grant MCP tools or A2A routes. Those routes require individual Vault identities with exactly one matching `agentgateway_access` value. See the [access table and operator setup in README](README.md#least-privilege-access). With a local Vault operator token, `scripts/mint-demo-identities.sh` creates calculator/text-tool and A2A entities plus an unmatched deny-test entity; it does not create the PostgreSQL reader identity. Create that separately with `vault write identity/entity name=demo-tools-postgres metadata=agentgateway_access=tools:postgres-read`. The helper does not map users or grant caller-side Vault write access. Have a trusted operator associate intended authenticated identities with the corresponding entities.

After minting short-lived JWTs for the route identities, make them available only in the current shell/process environment (do not save them in `.env`, shell history, screenshots, or logs). Set `JWT_TOOL_CALCULATE`, `JWT_TOOL_STATS`, `JWT_TOOL_REVERSE`, `JWT_TOOL_POSTGRES`, `JWT_AGENT_CALCULATOR`, `JWT_AGENT_TEXT_ANALYST`, `JWT_AGENT_TEXT_TRANSFORMER`, `JWT_AGENT_RESEARCH`, `JWT_AGENT_DATA_ANALYST`, `JWT_AGENT_ISSUE_TRIAGE`, and `JWT_NO_ACCESS`, then run:

```sh
python3 scripts/check-examples.py
```

This checks allowed calls, cross-tool denials, agent cards/messages, and rejection of missing, invalid, and valid-but-ungranted tokens. It also performs the research agent's allowed public fetch. To run the offline unit tests and Compose security check instead:

```sh
python3 -B -m unittest discover -s tests -v
python3 -B scripts/check-security.py
```

## 8. Use Codex through Agentgateway (optional)

In the authenticated shell, launch Codex with a temporary provider configuration:

```sh
codex -m gpt-6-luna \
  -c 'model_provider="agw"' \
  -c 'model_providers.agw={name="Agentgateway",base_url="http://localhost:4000/v1",env_key="JWT",wire_api="responses",requires_openai_auth=false}'
```

## 9. Stop the demo

```sh
docker compose down
```

This stops the services and preserves the local PostgreSQL volume. Use `docker compose down -v` only when you also intend to delete that database data. See [Security](SECURITY.md) for local-demo limitations.
