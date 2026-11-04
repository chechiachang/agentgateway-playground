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

Run in one shell: `eval` applies the `export JWT='…'` output so commands can use `$JWT`.

```sh
eval "$(make login)"
```

Open <http://localhost:4000/ui>, choose **Sign in with SSO**, and sign in with the configured GitHub account. The gateway checks organization membership and the configured username. Visit **Models** at <http://localhost:4000/ui/llm/models> to see `gpt-6-luna`, `gpt-6.1-sol`, `text-embedding-3-large`, and `decision-1`.

## 3. Exercise authenticated model routes

Codex calls Agentgateway's authenticated Responses API using `JWT`; inference uses Azure quota.

```sh
eval "$(make jwt)"  # run make login instead if the Vault session expired
codex -m gpt-6-luna \
  -c 'model_provider="agw"' \
  -c 'model_providers.agw={name="Agentgateway",base_url="http://localhost:4000/v1",env_key="JWT",wire_api="responses",requires_openai_auth=false}' \
  'Reply with hello from Agentgateway; do not use tools.'
```

Alternatively, merge [examples/codex-config.toml](examples/codex-config.toml) into `~/.codex/config.toml`, then run `codex` in the shell with `JWT` set; `--config` takes key/value overrides, not a file path. Use `-m gpt-6.1-sol` for the other Codex model.

Check auth, chat, embeddings, and native SystemOne separately:

```sh
sh scripts/check-auth.sh
sh scripts/check-models.sh
sh scripts/check-models.sh path/to/systemone-request.json  # optional custom payload
```

## 4. Demonstrate MCP tools

The playground needs a one-hour, tool-scoped Vault JWT; browser SSO and the normal model JWT grant no MCP tools.

```sh
mint_jwt() {
  docker compose exec -T vault sh -ec '
    export VAULT_TOKEN="$VAULT_DEV_ROOT_TOKEN_ID"
    entity_id=$(vault write -field=id identity/entity name="demo-${1%%:*}-${1#*:}" metadata="agentgateway_access=$1")
    VAULT_TOKEN=$(vault token create -entity-id="$entity_id" -policy=agentgateway-login -ttl=1h -field=token)
    export VAULT_TOKEN
    vault read -field=token identity/oidc/token/agentgateway-access
  ' sh "$1"
}
export JWT="$(mint_jwt tools:calculate)"
printf '%s' "$JWT" | pbcopy
```

Open <http://localhost:4000/ui/mcp/playground> → paste into **Bearer token** (no `Bearer ` prefix) → **Initialize** → select `playground_calculate` → arguments `{"expression":"(12 + 8) * 3"}` → **Call tool**; expect `60`. Rerun to refresh the token.

MCP is exposed at `http://localhost:4000/mcp`. It supports `tools/list` and `tools/call`; tool names are prefixed by their target. Use an identity granted only `tools:calculate` to list and call the calculator:

```sh
curl -sS http://localhost:4000/mcp \
  -H "Authorization: Bearer $JWT" \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'

curl -sS http://localhost:4000/mcp \
  -H "Authorization: Bearer $JWT" \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"playground_calculate","arguments":{"expression":"(12 + 8) * 3"}}}'
```

The other tools are `playground_text_stats` and `playground_reverse_text`; first set `export JWT="$(mint_jwt tools:text-stats)"` or `export JWT="$(mint_jwt tools:reverse-text)"`. One tool's JWT cannot call another; `eval "$(make jwt)"` restores the model JWT.

## 5. Demonstrate read-only PostgreSQL MCP

Use a separate identity with `tools:postgres-read`. Discover accessible tables with `postgres_postgres_list_tables`, then call the query tool:

```sh
export JWT="$(mint_jwt tools:postgres-read)"
curl -sS http://localhost:4000/mcp \
  -H "Authorization: Bearer $JWT" \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"postgres_postgres_list_tables","arguments":{}}}'

curl -sS http://localhost:4000/mcp \
  -H "Authorization: Bearer $JWT" \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"postgres_postgres_query","arguments":{"sql":"SELECT 1 AS value"}}}'
```

The demo database starts without sample tables. Add data using a trusted database owner, then use `postgres_postgres_describe_table` with a table returned by `postgres_postgres_list_tables`. The MCP reader can only SELECT; queries are single-statement, bounded to 200 rows, and subject to database-side read-only and timeout limits. Do not use admin credentials for MCP.

## 6. Call A2A agents

Agent cards are available at `/agents/<name>/.well-known/agent-card.json`; messages use A2A JSON-RPC `message/send`. For example, with an identity granted only `agents:calculator`:

```sh
export JWT="$(mint_jwt agents:calculator)"
curl -sS http://localhost:4000/agents/calculator \
  -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","id":1,"method":"message/send","params":{"message":{"kind":"message","role":"user","messageId":"demo-1","parts":[{"kind":"text","text":"(12 + 8) * 3"}]}}}'
```

For each agent, set `export JWT="$(mint_jwt agents:NAME)"`, replace the URL's agent name, and use its input:

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

The access checker needs separate identities; mint each into `$JWT`, then populate its required environment variable (section 4's local-operator helper).

```sh
for spec in \
  JWT_TOOL_CALCULATE=tools:calculate \
  JWT_TOOL_STATS=tools:text-stats \
  JWT_TOOL_REVERSE=tools:reverse-text \
  JWT_TOOL_POSTGRES=tools:postgres-read \
  JWT_AGENT_CALCULATOR=agents:calculator \
  JWT_AGENT_TEXT_ANALYST=agents:text-analyst \
  JWT_AGENT_TEXT_TRANSFORMER=agents:text-transformer \
  JWT_AGENT_RESEARCH=agents:research \
  JWT_AGENT_DATA_ANALYST=agents:data-analyst \
  JWT_AGENT_ISSUE_TRIAGE=agents:issue-triage \
  JWT_NO_ACCESS=none; do
  export JWT="$(mint_jwt "${spec#*=}")"
  export "${spec%%=*}=$JWT"
done
python3 scripts/check-examples.py
eval "$(make jwt)"  # restore the model JWT
```

This checks allowed calls, cross-tool denials, agent cards/messages, and rejection of missing, invalid, and valid-but-ungranted tokens. It also performs the research agent's allowed public fetch. To run the offline unit tests and Compose security check instead:

```sh
python3 -B -m unittest discover -s tests -v
python3 -B scripts/check-security.py
```

## 8. Stop the demo

```sh
docker compose down
```

This stops the services and preserves the local PostgreSQL volume. Use `docker compose down -v` only when you also intend to delete that database data. See [Security](SECURITY.md) for local-demo limitations.
