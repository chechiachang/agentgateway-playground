# Agentgateway demo

## Setup

```sh
make setup  # Python 3; generates secrets and private local configs
make model-catalog-refresh  # fetches and validates public model pricing
```

Set GitHub org/user, OAuth client credentials, and Azure API key in `.env`. [GitHub OAuth App](https://github.com/settings/developers) callback: `http://localhost:5556/dex/callback`; grant organization access.

```sh
docker compose up -d --force-recreate
```

## Login

```sh
eval "$(make login)"  # opens your browser; sign in with GitHub
eval "$(make jwt)"    # refresh JWT using the stored Vault session
```

## Gateway UI

Open <http://localhost:4000/ui> → **Sign in with SSO** → GitHub. The same org/user restrictions apply; port 15000 is disabled.

[Models](http://localhost:4000/ui/llm/models): `gpt-6-luna`, `gpt-6.1-sol`, `text-embedding-3-large`, `decision-1` (SystemOne: native `state`/`questions`, not chat messages).

## Model cost catalog

Refresh public pricing with `make model-catalog-refresh`. The validated catalog is atomically written to ignored `data/model-catalog/catalog.json` and mounted read-only at `/config/costs/catalog.json`; restart Agentgateway after refreshing so new requests use the updated prices. Only models with matching catalog prices are cost-accounted. The custom `decision-1` SystemOne model is unpriced and is not covered by the spend limit.

## Local model tests

After login (requires `curl` and Python 3):

```sh
sh scripts/check-auth.sh
sh scripts/check-models.sh                         # chat, embeddings, and SystemOne
sh scripts/check-models.sh systemone-request.json  # optional custom request
```

For local API-key access, send `x-api-key: test` (user `test`) or `x-api-key: test-ratelimit` (user `test-ratelimit`); both have group `sre` and can access the configured models. These demo keys are intentionally simple and not for deployment.

LLM requests use Envoy ratelimit backed by private Valkey: only `test-ratelimit` has a request-count limit (1 per minute). Each JWT username has a $100 spend budget per fixed 30-day month, and the `test` API-key username has a $10 budget, based on the refreshed model catalog. Unpriced models (including `decision-1`) are not cost-capped; only `test-ratelimit` remains subject to a request-count limit. Rate-limit state persists in the `valkey-data` volume, and enforcement fails closed if the rate-limit service is unavailable. See [ratelimit.md](ratelimit.md) for details.

To exercise the `test-ratelimit` request limit, send two requests within one minute; expect `200` followed by `429`:

```sh
for i in 1 2; do
  curl --silent --show-error --output /dev/null --write-out "request $i: HTTP %{http_code}\n" \\
    -H 'Content-Type: application/json' \\
    -H 'x-api-key: test-ratelimit' \\
    --data '{"model":"gpt-6-luna","messages":[{"role":"user","content":"ping"}],"stream":false}' \\
    http://localhost:4000/v1/chat/completions
done
```

## Guardrails

Strict demo guards are opt-in: request `gpt-6-luna-guarded` or `gpt-6.1-sol-guarded` (same underlying providers). They reject SSNs and `AGW_DEMO_SECRET_*` tokens in input (HTTP `403`) and mask non-streaming output. Streaming matches are blocked with an SSE error; earlier output cannot be retracted. Rules scan conversation history too, so even quoted examples can block resume. Normal chat models have no demo guards and remain usable for Codex. Authentication and rate limits apply to all models.

Rules live in the tracked [gateway config](config/agentgateway.yaml). Restart Agentgateway after changes. These local patterns are not a complete sensitive-data or prompt-injection defense.

```sh
docker compose restart agentgateway
python3 scripts/check-guardrails.py  # local test key, or exported JWT; rejected requests use no model quota
```

## Tools, MCP, and agents

MCP at `http://localhost:4000/mcp` exposes `playground_calculate` (arithmetic), `playground_text_stats` (counts), `playground_reverse_text` (Unicode text reversal), and PostgreSQL tools to list connectable databases, list tables/views across schemas, describe tables, and run bounded read-only queries. Table listing and queries can select a database; the default is `agentgateway`. Seven A2A agents accept `message/send` at `/agents/NAME`; cards are at each agent's `/.well-known/agent-card.json`. These deterministic examples use no model quota.

Every A2A route accepts a validated Vault JWT or configured caller API key only when its `groups` contains `nationalteam:sre`. SRE-group JWTs can use all MCP tools; other JWTs keep their per-tool grants. Agent-side model credentials are separate and are used only when an agent calls the AGW model API.

### Least-privilege access

Agentgateway validates the short-lived Vault JWT's issuer, `agentgateway` audience, expiry, subject, username, and signed groups claim, or accepts a configured caller API key. Every A2A route allows either credential when its groups contain `nationalteam:sre`; there are no per-agent caller keys. MCP grants all tools to SRE-group JWTs and uses per-tool allow rules for other JWTs. PostgreSQL remains read-only. The UI policy remains separate; it continues using its OIDC session restriction.

Vault maps Dex `preferred_username` into the signed JWT `username` claim. JWT validation requires `username`; request logs use the API-key username when present and otherwise the JWT username. Merge `standardAttributes.user` and the model auth policies from the example into existing configs; recreate Agentgateway, then run `make login` for a fresh token.

The `agentgateway-access` Vault OIDC role template places the operator-controlled `identity.entity.metadata.agentgateway_access` value into the signed `access` claim. The demo OIDC login can mint signed tokens, but its Vault policy grants no entity-metadata, key, role, or template write capability. Tokens use Vault's direct identity-token issuer (`http://localhost:8200/v1/identity/oidc`) and public keys endpoint; Agentgateway validates those matching values. Set metadata only through a trusted Vault operator. Use separate Vault entities per identity and assign exactly one value from this table:

| Identity | `agentgateway_access` value | Allowed route |
|---|---|---|
| Non-SRE tool caller | `tools:calculate` | only `playground_calculate` |
| Non-SRE tool caller | `tools:text-stats` | only `playground_text_stats` |
| Non-SRE tool caller | `tools:reverse-text` | only `playground_reverse_text` |
| Non-SRE database reader | `tools:postgres-read` | PostgreSQL list, describe, and read-only query tools |
| Non-SRE deny test identity | unset or any unmatched value, e.g. `none` | no MCP tools |

SRE-group JWTs receive all MCP tools regardless of `access`; other identities must be explicitly provisioned with the scoped claims shown above. The helper creates isolated demonstration entities and prints their IDs; set entity metadata only with an operator token, then map/login as that entity:

```sh
# Safe local setup: run only with your private .env value in the environment.
export VAULT_TOKEN="$(grep '^VAULT_DEV_ROOT_TOKEN_ID=' .env | cut -d= -f2-)"
scripts/mint-demo-identities.sh  # records entity IDs; does not grant caller write access
# Operator-only example for one entity ID; repeat with the exact value above:
eval "$(make login)"  # Vault JWT with the configured GitHub user's bound identity
eval "$(make jwt)"    # short-lived ID token; keep it in shell memory only
```

For isolated MCP smoke checks, set `JWT_TOOL_CALCULATE`, `JWT_TOOL_STATS`, `JWT_TOOL_REVERSE`, and `JWT_TOOL_POSTGRES` to valid scoped Vault JWTs, plus `JWT_NO_ACCESS` to an authenticated, non-SRE JWT without an MCP grant. Set `AGW_API_KEY` to a configured API key (defaults to local demo key `test`) for A2A checks. Optionally set `JWT_SRE` and `JWT_NO_SRE` to verify JWT allow/deny for the group policy. See [config](config/agentgateway.yaml) and [Vault bootstrap](scripts/vault-init.sh) for the JWT claim and policy wiring.

### PostgreSQL MCP: discovery and safety

The MCP endpoint is `/mcp`. MCP clients first call `tools/list`; the gateway returns only tools permitted by the caller's validated Vault JWT. `nationalteam:sre` identities see all tools. Other identities need `tools:postgres-read` to discover the three `postgres_postgres_*` tools. To discover gateway MCP endpoints, inspect the `mcp.targets` entries in `config/agentgateway.yaml` (or the template); tools themselves are discovered at runtime with MCP `tools/list`, not inferred from documentation. Gateway JWT validation and CEL allowlists are the enforcement boundary.

The Postgres service is private to the Compose network and has no published host port. PostgreSQL 18 uses the `postgres-data-v18` volume; an existing `postgres-data` volume is left untouched and is not migrated. Back up and restore existing data into PostgreSQL 18 explicitly. The MCP container gets only the reader connection string; the database admin password is never passed to the gateway or MCP. On initial database volume creation, Compose provisions `mcp_reader` with cluster database discovery/connect access, default read-only transactions, and SELECT-only rights in the initialized database; future tables created by the postgres owner inherit SELECT. MCP accepts only a single SELECT, disallows comments, semicolons, mutating/locking constructs, wraps results with a 200-row cap, and applies 5-second statement and 2-second lock timeouts. Database grants and read-only transactions are the final authority, not SQL filtering alone. Never use a real administrator credential for this MCP. `make setup` generates independent local admin/reader passwords; protect `.env` and database volumes. If credentials are changed after the volume was initialized, update the role in the database explicitly (init scripts run only on an empty volume).

Grant just `tools:postgres-read` to a dedicated Vault identity to permit all three read tools; leave the claim absent or unmatched to deny access. Each database tool can expose sensitive data and consume resources despite being read-only. Gateway authorization limits which identity can query, but does not filter rows or columns; use a narrow database view/schema and grants for finer boundaries. Data returned to the model may leave the machine via the configured model provider. DB-side read-only mode prevents writes, not data exfiltration or expensive reads. The separate MCP process/network and unprivileged DB account reduce blast radius; they add dependency/configuration complexity and an extra trusted component. This demo uses plain Compose-network PostgreSQL transport, suitable only for local trusted Docker networking, not production multi-host transport.

MCP targets and route policies live in [the gateway config](config/agentgateway.yaml).

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

### Codex coding agent

```sh
make coder  # optional Docker profile: pinned Codex CLI + local A2A adapter
```

The coder workspace is the isolated `codex-workspace` named volume, so delegated tasks cannot read this checkout or its `.env`. Populate it only with credential-free project files before delegating; the checkout is never mounted. `/agents/coder` uses the same `nationalteam:sre` caller authorization as every other agent. If it needs an LLM, its `CODEX_MODEL_API_KEY` authenticates the coder's model request to AGW; it is not the caller credential. Supports queued tasks, streamed progress, follow-ups, and cancellation. See [the agent list and delegation walkthrough](demo.md#delegate-a-job-to-coder) for commands and host mode.

### Specialist agents

The same Python stdlib service provides three deterministic A2A agents at `/agents/research`, `/agents/data-analyst`, and `/agents/issue-triage`; each card is published at `/agents/<name>/.well-known/agent-card.json`. All routes use the shared `nationalteam:sre` caller policy. Build/restart with `docker compose up -d --build`, then use the A2A `message/send` JSON-RPC interface or existing MCP tools.

- **Research** accepts one HTTPS URL. It only permits `www.iana.org`, `www.rfc-editor.org`, and `www.python.org`, uses port 443, checks every resolved address is globally routable, times out after 3 seconds, accepts only text/plain or text/html, caps responses at 64 KiB, and does not follow redirects. Output text is capped at 16 KiB. This is a small public-source fetcher, not a crawler.
- **Data analyst** accepts CSV or JSON (JSON array of objects) through `data_summary`; max 64 KiB input, 1,000 rows, 50 columns, and 4 KiB per field. Reports missing/distinct counts, numeric min/max/mean/median, or top categorical values. A2A message text is JSON: `{"format":"csv","data":"value\\n1\\n2\\n"}` (JSON format accepts `data` as an array or encoded JSON string).
- **Issue triage** picks only `platform`, `security`, `documentation`, or `product`, and returns a keyword-based rationale and heuristic confidence. It is a suggestion only: it cannot authorize, assign, or perform actions.

All three are covered by offline unit tests and need no model credentials. Run `python3 -B -m unittest discover -s tests -v`; after login, `python3 scripts/check-examples.py` also exercises their gateway cards/routes (the research request is intentionally not made against live internet).
