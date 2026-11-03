# Security

Local demo only: HTTP, in-memory dev Vault, and a root token accessible to Docker users. Do not expose ports remotely or use production credentials.

Review fixes:

- `make setup` generates independent random secrets, replaces published demo defaults, and writes `.env` with mode 0600. Existing real credentials/configs are preserved.
- Vault setup receives only its required environment; containers drop capabilities and prohibit privilege escalation.
- JWT output is validated before shell export. GitHub org/user restrictions protect login; strict JWT validation protects model APIs. Admin port is disabled; published ports bind loopback.
- Actions runs redacted Gitleaks history scans, Compose security checks, and Trivy secret/configuration/image scans on PRs, pushes, and weekly. HIGH/CRITICAL findings fail; no paid GitHub security feature required. Only the exact historical public demo session key is exempted in Gitleaks; real secrets are not.

Remaining: CLI Vault tokens are cached inside the Vault container; root tokens appear in dev logs. JWTs remain usable until expiry (one hour), without immediate revocation. Browser sessions may last longer. No inference spending/rate limits. Docker access and local processes are trusted. Scans do not prove absence of vulnerabilities.

After upgrading an existing checkout, run `make setup`, then `docker compose up -d --force-recreate` and log in again. Image findings require upstream fixes; do not bypass failed scans silently.

## Image scan (2026-11-02)

| Image | HIGH | CRITICAL |
|---|---:|---:|
| Vault 2.2.1 | 2 | 0 |
| Dex 2.46.1 | 22 | 0 |
| Agentgateway 1.7.0 | 0 | 0 |
| Example runtime (stdlib-only Python) | 0 | 0 |

Vault includes `CVE-2026-116587` in go-jose v3/v4 (crafted JWK RSA parameters can cause denial of service). Dex includes the same CVE plus dependency findings, mostly in the unused gomplate binary; CI intentionally scans the entire image. Requested Vault/Agentgateway versions are retained. `.trivyignore.yaml` temporarily ignores these image CVEs until 2026-11-16 (then CI fails again);  repository secrets and Compose checks passed.
