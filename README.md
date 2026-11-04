# Agentgateway demo

## Setup

```sh
make setup  # Python 3; generates secrets and copies missing configs
```

Set GitHub org/user, OAuth client credentials, and Azure API key in `.env`. [GitHub OAuth App](https://github.com/settings/developers) callback: `http://localhost:5556/dex/callback`; grant organization access.

```sh
docker compose up -d --force-recreate
```

## Login

```sh
eval "$(make login)"  # open the printed URL and sign in with GitHub
eval "$(make jwt)"    # refresh JWT using the stored Vault session
```

## Gateway UI

Open <http://localhost:4000/ui> → **Sign in with SSO** → GitHub. The same org/user restrictions apply; port 15000 is disabled.

[Models](http://localhost:4000/ui/llm/models): `gpt-6-luna`, `gpt-6.1-sol`, `text-embedding-3-large`, `decision-1` (SystemOne: native `state`/`questions`, not chat messages).

## Local model tests

After login (requires `curl` and Python 3):

```sh
sh scripts/check-auth.sh
sh scripts/check-models.sh                         # chat, embeddings, and SystemOne
sh scripts/check-models.sh systemone-request.json  # optional custom request
```

## Tools, MCP, and agents

MCP at `http://localhost:4000/mcp` exposes `playground_calculate` (arithmetic), `playground_text_stats` (counts), `playground_reverse_text` (Unicode text reversal), and the PostgreSQL tools `postgres_postgres_list_tables`, `postgres_postgres_describe_table`, and `postgres_postgres_query`. A2A agents at `/agents/calculator`, `/agents/text-analyst`, and `/agents/text-transformer` accept text via `message/send`; cards are at each agent's `/.well-known/agent-card.json`. These deterministic examples use no model quota.

### Least-privilege access

Agentgateway validates the short-lived Vault JWT's issuer, `agentgateway` audience, expiry, and subject before route authorization. The Vault OIDC role limits interactive login to the configured GitHub username (Dex also enforces org membership). Route-level CEL allow rules are allowlists: each A2A card and message route requires its exact `jwt.access` value; an otherwise valid JWT with no matching access claim is denied. MCP has separate per-tool allow rules; tools without an explicit match are not listed/usable. PostgreSQL is independently gated by the `tools:postgres-read` claim. Possessing a token for one tool/agent does not authorize any other route. The UI/model token policy remains separate; model routes retain strict JWT validation, while the UI continues using its OIDC session restriction.

The `agentgateway-access` Vault OIDC role template places the operator-controlled `identity.entity.metadata.agentgateway_access` value into the signed `access` claim. The demo OIDC login can mint signed tokens, but its Vault policy grants no entity-metadata, key, role, or template write capability. Tokens use Vault's direct identity-token issuer (`http://localhost:8200/v1/identity/oidc`) and public keys endpoint; Agentgateway validates those matching values. Set metadata only through a trusted Vault operator. Use separate Vault entities per identity and assign exactly one value from this table:

| Identity | `agentgateway_access` value | Allowed route |
|---|---|---|
| Tool caller | `tools:calculate` | only `playground_calculate` |
| Tool caller | `tools:text-stats` | only `playground_text_stats` |
| Tool caller | `tools:reverse-text` | only `playground_reverse_text` |
| Database reader | `tools:postgres-read` | PostgreSQL list, describe, and read-only query tools |
| Agent caller | `agents:calculator` | only `/agents/calculator` (card + message) |
| Agent caller | `agents:text-analyst` | only `/agents/text-analyst` |
| Agent caller | `agents:text-transformer` | only `/agents/text-transformer` |
| Agent caller | `agents:research` | only `/agents/research` |
| Agent caller | `agents:data-analyst` | only `/agents/data-analyst` |
| Agent caller | `agents:issue-triage` | only `/agents/issue-triage` |
| Deny test identity | unset or any unmatched value, e.g. `none` | no tools or agent routes |

The existing local demo's GitHub-bound Vault identity is intentionally not given a tool/agent grant. For a real deployment, provision individual Vault entities and OIDC aliases under operator control (or use an established IdP that emits the same `access` claim), and decide grants from an authenticated administrative process. The helper creates isolated demonstration entities and prints their IDs; set entity metadata only with an operator token, then map/login as that entity:

```sh
# Safe local setup: run only with your private .env value in the environment.
export VAULT_TOKEN="$(grep '^VAULT_DEV_ROOT_TOKEN_ID=' .env | cut -d= -f2-)"
scripts/mint-demo-identities.sh  # records entity IDs; does not grant caller write access
# Operator-only example for one entity ID; repeat with the exact value above:
vault write identity/entity/id/ENTITY_ID metadata=agentgateway_access=agents:calculator
eval "$(make login)"  # Vault JWT with the configured GitHub user's bound identity
eval "$(make jwt)"    # short-lived ID token; keep it in shell memory only
```

For isolated allowed/denied smoke tests, run the script with environment variables `JWT_TOOL_CALCULATE`, `JWT_TOOL_STATS`, `JWT_TOOL_REVERSE`, `JWT_TOOL_POSTGRES`, one `JWT_AGENT_<NAME>` per agent, and `JWT_NO_ACCESS` set to valid Vault JWTs minted from the corresponding separately provisioned entities. Do not put JWTs in `.env`, command history, screenshots, or logs. It tests each allowed tool/agent, cross-tool and no-grant denial, missing/invalid tokens for MCP, cards and A2A messages, and valid-but-ungranted access. `JWT` remains optional for unrelated model/UI smoke checks. See [config](config/agentgateway.yaml.example) and [Vault bootstrap](scripts/vault-init.sh) for the exact claim and policy wiring.

### PostgreSQL MCP: discovery and safety

The MCP endpoint is `/mcp`. MCP clients first call `tools/list`; the gateway returns only tools permitted by the caller's validated Vault JWT. Names include the MCP target prefix (`postgres_`), so an authorized `tools:postgres-read` identity discovers the three `postgres_postgres_*` tools. To discover gateway MCP endpoints, inspect the `mcp.targets` entries in `config/agentgateway.yaml` (or the template); tools themselves are discovered at runtime with MCP `tools/list`, not inferred from documentation. Authentication does not depend on agent self-declaration: gateway JWT validation and per-tool CEL allowlists are the enforcement boundary.

The Postgres service is private to the Compose network and has no published host port. The MCP container gets only the reader connection string; the database admin password is never passed to the gateway or MCP. On the initial database volume creation, Compose provisions a dedicated `mcp_reader` role with CONNECT, schema USAGE, and SELECT-only rights in `agentdb`; future tables created by the postgres owner inherit SELECT. MCP accepts only a single SELECT, disallows comments, semicolons, mutating/locking constructs, wraps results with a 200-row cap, and applies 5-second statement and 2-second lock timeouts. Database grants and read-only transactions are the final authority, not SQL filtering alone. Never use a real administrator credential for this MCP. `make setup` generates independent local admin/reader passwords; protect `.env` and database volumes. If credentials are changed after the volume was initialized, update the role in the database explicitly (init scripts run only on an empty volume).

Grant just `tools:postgres-read` to a dedicated Vault identity to permit all three read tools; leave the claim absent or unmatched to deny access. Each database tool can expose sensitive data and consume resources despite being read-only. Gateway authorization limits which identity can query, but does not filter rows or columns; use a narrow database view/schema and grants for finer boundaries. Data returned to the model may leave the machine via the configured model provider. DB-side read-only mode prevents writes, not data exfiltration or expensive reads. The separate MCP process/network and unprivileged DB account reduce blast radius; they add dependency/configuration complexity and an extra trusted component. This demo uses plain Compose-network PostgreSQL transport, suitable only for local trusted Docker networking, not production multi-host transport.

Existing setups: merge the `mcp` and `routes` sections from [the gateway template](config/agentgateway.yaml.example) into `config/agentgateway.yaml`. `make setup` preserves existing configs.

```sh
docker compose up -d --build
eval "$(make login)"
python3 scripts/check-examples.py  # requires the route-specific test JWT_* variables above
```

## Codex

Launch without changing global config:

```sh
eval "$(make login)"
codex -m gpt-6-luna \
  -c 'model_provider="agw"' \
  -c 'model_providers.agw={name="Agentgateway",base_url="http://localhost:4000/v1",env_key="JWT",wire_api="responses",requires_openai_auth=false}'
```

Alternatively, merge [the provider example](examples/codex-config.toml) into `~/.codex/config.toml`, then run `codex` after login. Provider/auth settings are not loaded from project config; `--config` accepts key/value overrides, not a file path.

Dev-only. Restarting Vault resets sessions; inference uses Azure quota. [Security](SECURITY.md).

### Specialist agents

The same Python stdlib service provides three deterministic, no-credential A2A agents at `/agents/research`, `/agents/data-analyst`, and `/agents/issue-triage`; each card is published at `/agents/<name>/.well-known/agent-card.json`. The routes inherit the gateway's strict Vault JWT requirement. Merge the updated routes from `config/agentgateway.yaml.example` into an existing local gateway config; `make setup` will not overwrite it. Build/restart with `docker compose up -d --build`, then use the A2A `message/send` JSON-RPC interface or existing MCP tools.

- **Research** accepts one HTTPS URL. It only permits `www.iana.org`, `www.rfc-editor.org`, and `www.python.org`, uses port 443, checks every resolved address is globally routable, times out after 3 seconds, accepts only text/plain or text/html, caps responses at 64 KiB, and does not follow redirects. Output text is capped at 16 KiB. This is a small public-source fetcher, not a crawler.
- **Data analyst** accepts CSV or JSON (JSON array of objects) through `data_summary`; max 64 KiB input, 1,000 rows, 50 columns, and 4 KiB per field. Reports missing/distinct counts, numeric min/max/mean/median, or top categorical values. A2A message text is JSON: `{"format":"csv","data":"value\\n1\\n2\\n"}` (JSON format accepts `data` as an array or encoded JSON string).
- **Issue triage** picks only `platform`, `security`, `documentation`, or `product`, and returns a keyword-based rationale and heuristic confidence. It is a suggestion only: it cannot authorize, assign, or perform actions.

All three are covered by offline unit tests and need no model credentials. Run `python3 -B -m unittest discover -s tests -v`; after login, `python3 scripts/check-examples.py` also exercises their gateway cards/routes (the research request is intentionally not made against live internet).
