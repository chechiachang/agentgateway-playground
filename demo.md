# Agentgateway feature walkthrough

This guide demonstrates the local Compose stack: GitHub-backed sign-in, the UI and model APIs, least-privilege MCP tools, read-only PostgreSQL access, six deterministic A2A agents, and an optional Codex coding agent. Public gateway ports stay on localhost; Vault is development-only.

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

Run in one shell: login opens your browser automatically; `eval` applies the `export JWT='…'` output so commands can use `$JWT`. If the browser cannot open, use the printed link.

```sh
eval "$(make login)"
```

Open <http://localhost:4000/ui>, choose **Sign in with SSO**, and sign in with the configured GitHub account. The gateway checks organization membership and the configured username. Visit **Models** at <http://localhost:4000/ui/llm/models> to see `gpt-6-luna`, `gpt-6.1-sol`, `text-embedding-3-large`, and `decision-1`.

### Include GitHub teams in JWT

Set `GITHUB_TEAMS=platform,security` in `.env` once (your team slugs); startup registers Vault aliases, and `groups` lists only memberships verified by Dex as `ORG:TEAM`. These groups grant no MCP/A2A access. Older `config/dex.yaml` files need `teamNameField: slug` under the GitHub connector's `config`.

```sh
docker compose up -d --no-deps --force-recreate dex vault-init
eval "$(make login)"
part=$(printf '%s' "$JWT" | cut -d. -f2)
case $((${#part} % 4)) in
  2) part="$part==" ;;
  3) part="$part=" ;;
esac
printf '%s' "$part" | tr '_-' '/+' | openssl base64 -d -A | jq .
```

The JWT template is [config/vault-jwt-template.json](config/vault-jwt-template.json). Vault dev storage resets on restart; Compose reapplies this configuration automatically. Team membership changes require a fresh login.

## 3. Exercise authenticated model routes

Test a model request with the Vault JWT from step 2; inference uses Azure quota:

```sh
curl -sS http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-6-luna","messages":[{"role":"user","content":"Reply with exactly OK."}],"stream":false}'
```

Or test with the local API key, without a JWT:

```sh
curl -sS http://localhost:4000/v1/chat/completions \
  -H 'x-api-key: test' \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-6-luna","messages":[{"role":"user","content":"Reply with exactly OK."}],"stream":false}'
```

Codex calls Agentgateway's authenticated Responses API using `JWT`.

```sh
make codex  # run make login first if the Vault session expired
```

Use `make codex CODEX_MODEL=gpt-6.1-sol` for the other model. Alternatively, merge [examples/codex-config.toml](examples/codex-config.toml) into `~/.codex/config.toml`, then run `codex` in the shell with `JWT` set; `--config` takes key/value overrides, not a file path.

Check auth, chat, embeddings, and native SystemOne separately:

```sh
sh scripts/check-auth.sh
sh scripts/check-models.sh
sh scripts/check-models.sh path/to/systemone-request.json  # optional custom payload
```

## Security: authentication and model guardrails

Authentication identifies the caller; authorization decides which routes/tools they may use; guardrails inspect model content. Policies are in [config/agentgateway.yaml](config/agentgateway.yaml).

| Credential | Header | Access |
|---|---|---|
| Vault JWT from step 2 | `Authorization: Bearer $JWT` | Model APIs; gateway verifies signature, issuer, `agentgateway` audience, expiry, and required identity claims |
| Local demo API key | `x-api-key: test` or `x-api-key: test-ratelimit` | Model APIs only; keys map to separate usernames and rate-limit budgets |
| Scoped Vault JWT | `Authorization: Bearer $JWT` | MCP/A2A only when its signed `access` claim matches the tool/route allowlist |

Model APIs accept either a valid JWT or a configured API key; neither credential means access is denied. Browser SSO is separate from API authentication. The simple API keys are public demo credentials, not deployment secrets.

### Opt-in model guardrails

| Model names | Content policy |
|---|---|
| `gpt-6-luna`, `gpt-6.1-sol` | No demo content guards; use for normal Codex sessions |
| `gpt-6-luna-guarded`, `gpt-6.1-sol-guarded` | Same providers, with strict local SSN and `AGW_DEMO_SECRET_[A-Z0-9]+` detection |

Guarded models reject matching input with HTTP `403`, mask matching non-streaming output, and block matching streaming windows with an SSE error. They scan system prompts, conversation history, tool results, and tool arguments—not only the latest prompt. Quoted examples can therefore block Codex resume; use normal models when working on these examples. Authentication and rate limits still apply to all models.

Demonstrate rejection with an API key (no model inference/quota is used):

```sh
curl -i http://localhost:4000/v1/chat/completions \
  -H 'x-api-key: test' \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-6-luna-guarded","messages":[{"role":"user","content":"Use this credential: AGW_DEMO_SECRET_123ABC"}],"stream":false}'
```

Expect `403 Forbidden` with `Guardrail rejected sensitive input.` To use JWT authentication instead, replace the API-key header with `-H "Authorization: Bearer $JWT"`. The prompt `My SSN is 123-45-6789` also triggers rejection.

```sh
python3 scripts/check-guardrails.py  # both guarded models; chat and Responses history
```

These patterns are a demonstration, not a complete sensitive-data or prompt-injection defense. Streaming checks cannot retract already-delivered output.

## 4. Demonstrate MCP tools

The playground needs a one-hour, tool-scoped Vault JWT; browser SSO and the normal model JWT grant no MCP tools.

```sh
mint_jwt() {
  docker compose exec -T vault sh -ec '
    export VAULT_TOKEN="$VAULT_DEV_ROOT_TOKEN_ID"
    name="demo-${1%%:*}-${1#*:}"
    vault write identity/entity name="$name" metadata="agentgateway_access=$1" >/dev/null
    entity_id=$(vault read -field=id "identity/entity/name/$name")
    accessor=$(vault read -field=accessor sys/auth/token)
    vault write identity/entity-alias name="$name" canonical_id="$entity_id" mount_accessor="$accessor" >/dev/null
    vault write "auth/token/roles/$name" allowed_entity_aliases="$name" allowed_policies=agentgateway-login token_explicit_max_ttl=1h >/dev/null
    VAULT_TOKEN=$(vault token create -role="$name" -entity-alias="$name" -policy=agentgateway-login -no-default-policy -ttl=1h -field=token)
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

The `demo-tools-agents` service hosts six deterministic agents, not a kagent controller; use HTTP card discovery and `message/send` ([kagent A2A example](https://kagent.dev/docs/kagent/1.x/examples/a2a-agents/)), with section 4's helper:

```sh
export JWT="$(mint_jwt agents:calculator)"
curl -sS http://localhost:4000/agents/calculator/.well-known/agent-card.json \
  -H "Authorization: Bearer $JWT"

curl -sS http://localhost:4000/agents/calculator \
  -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","id":1,"method":"message/send","params":{"message":{"kind":"message","role":"user","messageId":"demo-1","parts":[{"kind":"text","text":"(12 + 8) * 3"}]}}}'
```

Expect an agent message containing `{"value":60}`. Unlike kagent's controller, these demos have no gRPC, streaming, persisted sessions/tasks, or human-in-the-loop support. Routes and exact-access authorization are defined in [config/agentgateway.yaml](config/agentgateway.yaml); restart Agentgateway after changing it.

### List agents

These are the configured agents; `coder` runs only when its Compose profile is started. Every endpoint is `/agents/NAME`; its card is `/agents/NAME/.well-known/agent-card.json`. A2A has no global list endpoint in this demo. Inspect configured routes with `rg -n 'pathPrefix: /agents/' config/agentgateway.yaml`, then fetch an agent's card with its scoped JWT.

For each agent, set `export JWT="$(mint_jwt agents:NAME)"`, replace the URL's agent name, and use its input:

| Agent | Example input | Behavior |
|---|---|---|
| `calculator` | `(12 + 8) * 3` | Bounded arithmetic |
| `text-analyst` | `Hello from agentgateway` | Character, word, and line counts |
| `text-transformer` | `agentgateway` | Reverses Unicode text |
| `research` | `https://www.iana.org/domains/reserved` | Fetches a small public page from the HTTPS allowlist |
| `data-analyst` | `{"format":"csv","data":"team,score\nplatform,7\nsecurity,9\n"}` | Summarizes CSV or JSON records |
| `issue-triage` | `Security vulnerability with leaked credential` | Suggests a team using keywords |
| `coder` (optional) | `Fix the failing tests in this project` | Local Codex CLI edits the mounted project; queues tasks and streams progress |

Research makes an outbound request and only permits `www.iana.org`, `www.rfc-editor.org`, and `www.python.org`; it does not follow redirects. Data analysis has bounded input/row/column sizes. Issue triage is heuristic guidance only, not authorization or an action.

### Delegate a job to coder

```text
You or another agent → AGW /agents/coder → A2A adapter → Codex CLI → mounted project
                                                          ↓
                                                   AGW model API
```

AGW authenticates and routes requests; it does not schedule jobs itself. A caller delegates by submitting an A2A message. The adapter keeps Codex running and serializes tasks against one workspace.

1. Set `CODEX_WORKSPACE` in `.env` to the project directory (default `.`), then start the containerized adapter and pinned Codex CLI:

   ```sh
   make coder
   docker compose --profile coder ps codex-agent
   docker compose --profile coder logs -f codex-agent
   ```

   The container runs as UID 1000, publishes no backend port, and mounts only the selected project writable plus a `codex-state` volume. Its root filesystem is read-only; capabilities are dropped and privilege escalation is disabled. Codex uses `externalSandbox` because Docker supplies this isolation, without requiring nested user namespaces or privileged Docker mode. `CODEX_MODEL` defaults to `gpt-6-luna`; model calls use AGW with the local `CODEX_MODEL_API_KEY=test` key. Additional project runtimes belong in [the coder image](examples/codex/Dockerfile).

2. Use section 4's local-operator helper to mint a dedicated coder JWT, then discover the card:

   ```sh
   export JWT="$(mint_jwt agents:coder)"
   curl -fsS http://localhost:4000/agents/coder/.well-known/agent-card.json \
     -H "Authorization: Bearer $JWT"
   ```

   The normal GitHub/model JWT and the model API key cannot delegate coding jobs. AGW also authenticates its backend connection using the private `CODEX_AGENT_TOKEN` generated by `make setup`.

3. Submit a bounded instruction describing the change and its acceptance check:

   ```sh
   python3 scripts/coder-task.py send \
     "Fix the failing calculator test. Run the relevant tests and summarize the diff."
   ```

   The response contains `result.id` (task ID), `result.contextId` (conversation ID), and `result.status.state`. Copy the returned IDs for the commands below:

   ```sh
   python3 scripts/coder-task.py get TASK_ID
   python3 scripts/coder-task.py send "Review your change for edge cases" --context CONTEXT_ID
   python3 scripts/coder-task.py cancel TASK_ID
   ```

   States are `submitted`, `working`, and then `completed`, `failed`, or `canceled`. Completed tasks expose `response` and, when provided by Codex, `diff` artifacts. Cancellation is confirmed after Codex interrupts the active turn; canceling a queued task prevents it from running. Review the actual project diff before committing.

4. To watch a new task as it runs, use streaming:

   ```sh
   python3 scripts/coder-task.py stream "Run the project tests and report the result"
   ```

   For another agent or an A2A client, send this JSON-RPC request to `http://localhost:4000/agents/coder` with `Authorization: Bearer $JWT` and `Content-Type: application/json`:

   ```json
   {
     "jsonrpc": "2.0",
     "id": 1,
     "method": "message/send",
     "params": {
       "configuration": {"blocking": false},
       "message": {
         "role": "user",
         "messageId": "unique-job-id",
         "parts": [{"kind": "text", "text": "Fix the failing tests and summarize the diff."}]
       }
     }
   }
   ```

   Follow-ups put the returned `contextId` inside `message`; use a new `messageId`. Retrying the same retained `messageId` with identical input returns the same task without executing it twice. Poll with `tasks/get` and `params={"id":"TASK_ID"}`; cancel with `tasks/cancel`. `message/stream` and `tasks/resubscribe` return SSE task/status/artifact events.

Operational limits: one active task, 16 queued tasks, 128 retained tasks/contexts, and a one-hour turn timeout. IDs are in memory and reset when the adapter restarts; Codex state and workspace edits persist. No interactive approval interface is provided: extra permissions are declined, and unsupported questions fail the task. A caller's instructions cannot change the configured workspace.

Optional live check: `python3 scripts/check-coder.py` uses the exported coder JWT and model quota, verifies a workspace write and same-context follow-up, then removes its test file. Set `CODEX_WORKSPACE` in that shell too if the container uses a different project.

Host alternative: install Codex CLI and Python 3, set `CODEX_AGENT_BACKEND=host.docker.internal:8082` in `.env`, run `docker compose up -d agentgateway`, and start `make coder-host`. This uses Codex's `workspace-write` sandbox and binds the authenticated adapter to host port 8082 for Docker access. Do not run host and Docker adapters against the same project concurrently.

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
docker compose --profile coder down
```

This stops all services, including the optional coder, and preserves PostgreSQL and Codex-state volumes. Add `-v` only when you intend to delete those volumes; it does not undo edits in the mounted project. See [Security](SECURITY.md) for local-demo limitations.
