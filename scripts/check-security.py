#!/usr/bin/env python3
"""Check demo Compose security invariants without using real credentials."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("setup", ROOT / "scripts/setup-env.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)

with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    shutil.copytree(ROOT / "config", root / "config", ignore=shutil.ignore_patterns("*.yaml", "vault.hcl"))
    shutil.copyfile(ROOT / ".env.example", root / ".env.example")
    shutil.copyfile(ROOT / "compose.yaml", root / "compose.yaml")
    setup.setup(root)
    with (root / ".env").open("a") as handle:
        handle.write("\nGITHUB_ORG=test-org\nGITHUB_USER=test-user\nGITHUB_OAUTH_CLIENT_ID=test-id\nGITHUB_OAUTH_CLIENT_SECRET=test-secret\n")
    result = subprocess.run(["docker", "compose", "--project-directory", str(root), "config", "--format", "json"], check=True, capture_output=True, text=True)
    services = json.loads(result.stdout)["services"]
    for name, service in services.items():
        assert service.get("privileged", False) is False, name
        assert "ALL" in service["cap_drop"], name
        assert "no-new-privileges:true" in service["security_opt"], name
        for port in service.get("ports", []):
            assert port["host_ip"] == "127.0.0.1", name
        for volume in service.get("volumes", []):
            if volume.get("type") == "volume":
                continue  # PostgreSQL data volume must be writable; bind mounts remain read-only.
            assert volume.get("read_only"), name
    assert set(services["vault-init"]["environment"]) == {"VAULT_ADDR", "VAULT_TOKEN", "GITHUB_ORG", "GITHUB_USER", "DEX_VAULT_CLIENT_SECRET"}
    assert services["postgres"].get("ports", []) == []
    assert services["postgres-mcp"].get("ports", []) == []
    assert services["postgres-mcp"]["environment"]["DATABASE_URL"].startswith("postgres://mcp_reader:")
    assert "POSTGRES_ADMIN_PASSWORD" not in services["postgres-mcp"]["environment"]
    env = services["agentgateway"]["environment"]
    assert env["OIDC_COOKIE_SECRET"] != env["AGENTGATEWAY_SESSION_KEY"]
print("PASS Compose: loopback ports, restricted privileges, read-only mounts, least-secret setup, independent keys")
