#!/usr/bin/env python3
"""Load only coder settings from .env, then start the host adapter."""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
for line in (ROOT / ".env").read_text().splitlines():
    key, separator, value = line.partition("=")
    if separator and key in {"CODEX_AGENT_TOKEN", "CODEX_WORKSPACE", "CODEX_MODEL", "CODEX_MODEL_API_KEY"}:
        os.environ.setdefault(key, value.strip().strip("\"'"))
os.environ.setdefault("CODEX_AGENT_HOST", "0.0.0.0")
os.execv(sys.executable, [sys.executable, "-B", str(ROOT / "examples/codex-agent.py")])
