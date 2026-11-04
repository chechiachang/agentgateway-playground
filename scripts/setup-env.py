#!/usr/bin/env python3
"""Create local config and private secrets without overwriting real credentials."""
import os
from pathlib import Path
import secrets
import tempfile

ROOT = Path(__file__).resolve().parent.parent
KEYS = {
    "VAULT_DEV_ROOT_TOKEN_ID", "DEX_VAULT_CLIENT_SECRET",
    "DEX_AGENTGATEWAY_CLIENT_SECRET", "AGENTGATEWAY_SESSION_KEY",
    "OIDC_COOKIE_SECRET", "POSTGRES_ADMIN_PASSWORD", "MCP_DB_PASSWORD",
}
PUBLIC_DEFAULTS = {
    "demo-root-token", "demo-vault-oidc-client-secret",
    "demo-agentgateway-oidc-client-secret",
    "0123456789abcdef" * 4,
}


def setup(root):
    path = root / ".env"
    source = path if path.exists() else root / ".env.example"
    lines = source.read_text().splitlines()
    seen = set()
    rotated = False
    for index, line in enumerate(lines):
        key, sep, value = line.partition("=")
        if sep and key in KEYS:
            seen.add(key)
            normalized = value.strip().strip("\"'")
            if not normalized or normalized in PUBLIC_DEFAULTS:
                lines[index] = key + "=" + secrets.token_hex(32)
                rotated = True
    for key in sorted(KEYS - seen):
        lines.append(key + "=" + secrets.token_hex(32))
        rotated = True
    fd, temporary = tempfile.mkstemp(dir=root, prefix=".env-setup-")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write("\n".join(lines) + "\n")
        os.replace(temporary, path)  # mkstemp permissions are 0600
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    for name in ("agentgateway.yaml", "dex.yaml", "vault.hcl"):
        destination = root / "config" / name
        if not destination.exists():
            destination.write_bytes(destination.with_suffix(destination.suffix + ".example").read_bytes())
    print("Local configuration ready; edit GitHub/Azure credentials in .env.")
    if rotated:
        print("Secrets generated/rotated. Recreate containers to apply; Vault restart resets login.")


if __name__ == "__main__":
    setup(ROOT)
