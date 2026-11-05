#!/usr/bin/env python3
"""Live coder smoke check; uses model quota and creates/removes one test file."""
import json
import os
import subprocess
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

BASE = os.environ.get("AGW_URL", "http://localhost:4000").rstrip("/")
AUTH_HEADERS = ({"Authorization": "Bearer " + os.environ["JWT"]}
                if os.environ.get("JWT") else
                {"x-api-key": os.environ.get("AGW_API_KEY", "test")})


def rpc(method, params):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    request = Request(BASE + "/agents/coder", data=json.dumps(body).encode(),
                      headers={**AUTH_HEADERS, "Content-Type": "application/json"})
    with urlopen(request, timeout=30) as response:
        result = json.load(response)
    if "error" in result:
        raise RuntimeError(result["error"]["message"])
    return result["result"]


def send(text, context=None):
    message = {"role": "user", "messageId": str(uuid4()), "parts": [{"kind": "text", "text": text}]}
    if context:
        message["contextId"] = context
    return rpc("message/send", {"configuration": {"blocking": False}, "message": message})


def wait(task):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        result = rpc("tasks/get", {"id": task["id"]})
        if result["status"]["state"] in ("completed", "failed", "canceled"):
            if result["status"]["state"] != "completed":
                raise AssertionError(result["status"])
            return result
        time.sleep(1)
    rpc("tasks/cancel", {"id": task["id"]})
    raise AssertionError("Coder smoke task timed out (cancellation requested)")


filename = ".coder-smoke-" + uuid4().hex + ".txt"
path = "/workspace/" + filename
marker = "agw-coder-" + uuid4().hex
try:
    for suffix, body in (("/.well-known/agent-card.json", None), ("", {"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": "none"}})):
        request = Request(BASE + "/agents/coder" + suffix,
                          data=None if body is None else json.dumps(body).encode(),
                          headers={"Content-Type": "application/json"})
        try:
            urlopen(request, timeout=10).close()
        except HTTPError as error:
            assert error.code in (401, 403), error.code
            error.close()
        else:
            raise AssertionError("Unauthenticated coder request accepted")
    card_request = Request(BASE + "/agents/coder/.well-known/agent-card.json", headers=AUTH_HEADERS)
    with urlopen(card_request, timeout=10) as response:
        assert json.load(response)["capabilities"]["streaming"]
    print("PASS coder discovery and missing-token denial", flush=True)
    instruction = (f"Adapter smoke test: run a Python 3 shell command to create the file {filename} "
        f"in the current workspace with exactly this content: {marker}. "
        "Read that file to verify its content, then reply with its content. "
        "Do not read or modify any other project files.")
    completed = wait(send(instruction))
    content = subprocess.run(
        ["docker", "compose", "--profile", "coder", "exec", "-T", "codex-agent", "cat", path],
        check=True, capture_output=True, text=True,
    ).stdout
    assert content == marker, "Codex did not write the expected workspace file"
    assert marker in json.dumps(completed["artifacts"]), "Missing result artifact"
    print("PASS live Codex tool execution, workspace write, and result artifact", flush=True)
    followup = wait(send("What exact content did you just write? Reply with that content only; do not run tools.", completed["contextId"]))
    assert marker in json.dumps(followup["artifacts"]), "Follow-up lost conversation state"
    print("PASS same-context follow-up", flush=True)
finally:
    subprocess.run(
        ["docker", "compose", "--profile", "coder", "exec", "-T", "codex-agent", "rm", "-f", "--", path],
        check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
