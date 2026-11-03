#!/usr/bin/env python3
"""Exercise MCP tools and A2A agents through AGW after make login (stdlib only)."""
import json
import os
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

BASE_URL = os.environ.get("AGW_URL", "http://localhost:4000").rstrip("/")
JWT = os.environ.get("JWT")  # Optional broad token retained for model/UI smoke checks.
SESSION = None


def request(path, body=None, token=None):
    global SESSION
    headers = {"Accept": "application/json, text/event-stream"}
    token = (JWT or "") if token is None else token
    if token:
        headers["Authorization"] = "Bearer " + token
    if path == "/mcp" and SESSION:
        headers["Mcp-Session-Id"] = SESSION
        headers["MCP-Protocol-Version"] = "2025-06-18"
    if body is not None:
        headers["Content-Type"] = "application/json"
    data = None if body is None else json.dumps(body).encode()
    with urlopen(Request(BASE_URL + path, data=data, headers=headers), timeout=20) as response:
        if path == "/mcp" and token:
            SESSION = response.headers.get("Mcp-Session-Id", SESSION)
        text = response.read().decode()
        if not text:
            return None
        if "text/event-stream" in response.headers.get("Content-Type", ""):
            for event in text.replace("\r\n", "\n").split("\n\n"):
                payload = "\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith("data:"))
                if payload:
                    result = json.loads(payload)
                    if result.get("id") == body.get("id"):
                        return result
            raise AssertionError("Missing MCP response in event stream")
        return json.loads(text)


def rpc(path, method, params, request_id=1, token=None):
    result = request(path, {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}, token=token)
    assert result is not None and "error" not in result, result
    return result["result"]


# Every identity has exactly one route-class grant; a second identity has a
# valid gateway JWT but no access claim to assert deny-by-default behavior.
identities = {
    "tool-calculate": "tools:calculate",
    "tool-stats": "tools:text-stats",
    "tool-reverse": "tools:reverse-text",
    "agent-calculator": "agents:calculator",
    "agent-text-analyst": "agents:text-analyst",
    "agent-text-transformer": "agents:text-transformer",
    "agent-research": "agents:research",
    "agent-data-analyst": "agents:data-analyst",
    "agent-issue-triage": "agents:issue-triage",
}
TOKENS = {name: os.environ.get("JWT_" + name.upper().replace("-", "_")) for name in identities}
if any(not token for token in TOKENS.values()):
    sys.exit("Set all route-specific JWT_* variables from the README identity-minting instructions")
no_access = os.environ.get("JWT_NO_ACCESS")
if not no_access:
    sys.exit("Set JWT_NO_ACCESS to an authenticated identity with no agentgateway_access metadata")

# Transport authentication rejects absent/invalid bearer tokens on each route class.
probe_paths = [
    ("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}),
    ("/agents/calculator/.well-known/agent-card.json", None),
    ("/agents/calculator", {"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {}}),
] + [(f"/agents/{name}/.well-known/agent-card.json", None) for name in (
    "text-analyst", "text-transformer", "research", "data-analyst", "issue-triage")]
for path, body in probe_paths:
    for token, label in (("", "no token"), ("invalid", "invalid token")):
        try:
            request(path, body, token=token)
        except HTTPError as error:
            assert error.code in (401, 403), (path, label, error.code)
        else:
            raise AssertionError(f"{label} accepted for {path}")
print("PASS missing/invalid bearer rejected for MCP, agent card, and A2A message routes")

# Per-tool identities can only discover/call their granted MCP tool; calls to
# either neighboring tools or an unknown tool must be rejected by MCP policy.
def mcp_rpc(method, params, token, request_id):
    return request("/mcp", {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}, token=token)

def mcp_call(tool_name, args, token, request_id):
    return mcp_rpc("tools/call", {"name": tool_name, "arguments": args}, token, request_id)

for identity, allowed_suffix, args, expected in [
    ("tool-calculate", "calculate", {"expression": "(12 + 8) * 3"}, {"value": 60}),
    ("tool-stats", "text_stats", {"text": "Hello from agentgateway"}, {"characters": 23, "words": 3, "lines": 1}),
    ("tool-reverse", "reverse_text", {"text": "agentgateway"}, {"text": "yawetagtnega"}),
]:
    token = TOKENS[identity]
    # Current gateway policy may filter denied tools in tools/list; it must deny
    # a direct call as well. Compare actual public names without leaking JWTs.
    visible = mcp_rpc("tools/list", {}, token, 10)["result"]["tools"]
    allowed_name = next(tool["name"] for tool in visible if tool["name"].endswith(allowed_suffix))
    result = mcp_call(allowed_name, args, token, 11)["result"]
    assert not result.get("isError"), result
    assert json.loads(result["content"][0]["text"]) == expected, result
    for denied_name in ("playground_calculate", "playground_text_stats", "playground_reverse_text"):
        if denied_name == allowed_name:
            continue
        try:
            mcp_call(denied_name, {"expression": "1+1", "text": "x"}, token, 12)
        except HTTPError as error:
            assert error.code in (401, 403), (identity, denied_name, error.code)
        else:
            raise AssertionError(f"{identity} unexpectedly called {denied_name}")
    try:
        mcp_rpc("tools/call", {"name": "playground_unknown", "arguments": {}}, token, 13)
    except HTTPError as error:
        assert error.code in (401, 403), (identity, "unknown", error.code)
    else:
        raise AssertionError(f"{identity} unexpectedly called unknown tool")
    print(f"PASS MCP {identity}: allow {allowed_suffix}; deny other/unknown tool")

# A valid identity without a grant is denied for every tool and agent route.
no_access_paths = [
    ("/mcp", {"jsonrpc": "2.0", "id": 20, "method": "tools/list", "params": {}}),
    ("/agents/calculator/.well-known/agent-card.json", None),
    ("/agents/calculator", {"jsonrpc": "2.0", "id": 21, "method": "message/send", "params": {}}),
] + [(f"/agents/{name}/.well-known/agent-card.json", None) for name in (
    "text-analyst", "text-transformer", "research", "data-analyst", "issue-triage")]
for path, body in no_access_paths:
    try:
        request(path, body, token=no_access)
    except HTTPError as error:
        assert error.code in (401, 403), (path, error.code)
    else:
        raise AssertionError(f"Valid identity without an access grant accepted for {path}")
print("PASS valid JWT without access claim denied by default for MCP, all agent cards and A2A routes")



for name, text, expected in [
    ("calculator", "(12 + 8) * 3", {"value": 60}),
    ("text-analyst", "Hello from agentgateway", {"characters": 23, "words": 3, "lines": 1}),
    ("text-transformer", "agentgateway", {"text": "yawetagtnega"}),
    ("research", "https://www.iana.org/", None),
    ("data-analyst", json.dumps({"format": "csv", "data": "n\n1\n3\n"}), {"rows": 2}),
    ("issue-triage", "Deployment gateway crash", {"team": "platform"}),
]:
    path = "/agents/" + name
    identity = "agent-" + name
    token = TOKENS[identity]
    card = request(path + "/.well-known/agent-card.json", token=token)
    assert card["url"].endswith(path) and card["security"], card
    result = rpc(path, "message/send", {"message": {
        "kind": "message", "role": "user", "messageId": str(uuid4()),
        "parts": [{"kind": "text", "text": text}],
    }}, token=token)
    parsed_result = json.loads(result["parts"][0]["text"])
    if name == "research":
        assert "text" in parsed_result and len(parsed_result["text"]) <= 16384, parsed_result
    elif name == "data-analyst":
        assert parsed_result["rows"] == expected["rows"], parsed_result
    elif name == "issue-triage":
        assert parsed_result["team"] == expected["team"] and parsed_result["heuristic"], parsed_result
    else:
        assert parsed_result == expected, result
    assert result["role"] == "agent", result
    print(f"PASS A2A {name}")
