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
    shutil.copytree(ROOT / "config", root / "config", ignore=shutil.ignore_patterns("*.backup-*"))
    shutil.copyfile(ROOT / ".env.example", root / ".env.example")
    shutil.copyfile(ROOT / "compose.yaml", root / "compose.yaml")
    setup.setup(root)
    with (root / ".env").open("a") as handle:
        handle.write("\nGITHUB_ORG=test-org\nGITHUB_USER=test-user\nGITHUB_OAUTH_CLIENT_ID=test-id\nGITHUB_OAUTH_CLIENT_SECRET=test-secret\n")
    result = subprocess.run(["docker", "compose", "--profile", "coder", "--project-directory", str(root), "config", "--format", "json"], check=True, capture_output=True, text=True)
    services = json.loads(result.stdout)["services"]
    assert services["postgres"]["image"].startswith("postgres:18"), "PostgreSQL must remain on version 18"
    assert any(volume.get("type") == "volume" and volume["source"] == "postgres-data-v18" and volume["target"] == "/var/lib/postgresql" for volume in services["postgres"]["volumes"])
    for name, service in services.items():
        assert service.get("privileged", False) is False, name
        assert "ALL" in service["cap_drop"], name
        assert "no-new-privileges:true" in service["security_opt"], name
        for port in service.get("ports", []):
            assert port["host_ip"] == "127.0.0.1", name
        for volume in service.get("volumes", []):
            if volume.get("type") == "volume":
                continue  # PostgreSQL data volume must be writable; bind mounts remain read-only.
            if name == "codex-agent" and volume.get("target") == "/workspace":
                assert not volume.get("read_only"), name  # Explicit coding workspace only.
            else:
                assert volume.get("read_only"), name
    assert set(services["vault-init"]["environment"]) == {"VAULT_ADDR", "VAULT_TOKEN", "GITHUB_ORG", "GITHUB_USER", "GITHUB_TEAMS", "DEX_VAULT_CLIENT_SECRET"}
    assert services["postgres"].get("ports", []) == []
    assert services["postgres-mcp"].get("ports", []) == []
    assert services["postgres-mcp"]["environment"]["DATABASE_URL"].startswith("postgres://mcp_reader:")
    assert "POSTGRES_ADMIN_PASSWORD" not in services["postgres-mcp"]["environment"]
    env = services["agentgateway"]["environment"]
    assert env["OIDC_COOKIE_SECRET"] != env["AGENTGATEWAY_SESSION_KEY"]
    coder = services["codex-agent"]
    workspace = next(volume for volume in coder["volumes"] if volume["target"] == "/workspace")
    assert workspace["type"] == "volume" and workspace["source"] == "codex-workspace"
    assert coder.get("ports", []) == []
    assert coder["read_only"] is True
    assert coder["user"] == "1000:1000"
    assert coder["environment"]["CODEX_AGENT_TOKEN"] == env["CODEX_AGENT_TOKEN"]
    assert len(env["CODEX_AGENT_TOKEN"]) >= 32
    assert coder["environment"]["CODEX_AGENT_SANDBOX"] == "container"
    assert set(coder["environment"]) == {"CODEX_AGENT_HOST", "CODEX_AGENT_SANDBOX", "CODEX_AGENT_TOKEN", "CODEX_WORKSPACE", "CODEX_MODEL", "CODEX_MODEL_BASE_URL", "CODEX_MODEL_API_KEY"}
print("PASS Compose: loopback ports, restricted privileges, read-only mounts except coder workspace, least-secret setup, independent keys")
