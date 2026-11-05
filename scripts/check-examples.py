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


def request(path, body=None, token=None, api_key=None):
    global SESSION
    headers = {"Accept": "application/json, text/event-stream"}
    token = (JWT or "") if token is None else token
    if token:
        headers["Authorization"] = "Bearer " + token
    if api_key:
        headers["x-api-key"] = api_key
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


def rpc(path, method, params, request_id=1, token=None, api_key=None):
    result = request(path, {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}, token=token, api_key=api_key)
    assert result is not None and "error" not in result, result
    return result["result"]


# MCP uses scoped JWTs; A2A accepts any caller credential in nationalteam:sre.
identities = {
    "tool-calculate": "tools:calculate",
    "tool-stats": "tools:text-stats",
    "tool-reverse": "tools:reverse-text",
    "tool-postgres": "tools:postgres-read",
}
TOKENS = {name: os.environ.get("JWT_" + name.upper().replace("-", "_")) for name in identities}
if any(not token for token in TOKENS.values()):
    sys.exit("Set JWT_TOOL_CALCULATE, JWT_TOOL_STATS, JWT_TOOL_REVERSE, and JWT_TOOL_POSTGRES")
no_access = os.environ.get("JWT_NO_ACCESS")
if not no_access:
    sys.exit("Set JWT_NO_ACCESS to an authenticated identity with no tool access")

AGENT_NAMES = ("calculator", "text-analyst", "text-transformer", "research", "data-analyst", "issue-triage")
AGW_API_KEY = os.environ.get("AGW_API_KEY", "test")
SRE_TOKEN = os.environ.get("JWT_SRE")
no_sre = os.environ.get("JWT_NO_SRE")

# MCP rejects missing/invalid JWTs; A2A rejects missing/invalid credentials.
try:
    request("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, token="")
except HTTPError as error:
    assert error.code in (401, 403), error.code
else:
    raise AssertionError("Missing MCP JWT accepted")
for name in AGENT_NAMES:
    path = f"/agents/{name}/.well-known/agent-card.json"
    for token, api_key, label in (("", None, "missing credential"), ("", "invalid", "invalid API key")):
        try:
            request(path, token=token, api_key=api_key)
        except HTTPError as error:
            assert error.code in (401, 403), (path, label, error.code)
        else:
            raise AssertionError(f"{label} accepted for {path}")
    if no_sre:
        try:
            request(path, token=no_sre)
        except HTTPError as error:
            assert error.code in (401, 403), (path, "JWT without nationalteam:sre", error.code)
        else:
            raise AssertionError(f"JWT without nationalteam:sre accepted for {path}")
    if SRE_TOKEN:
        card = request(path, token=SRE_TOKEN)
        assert card["url"].endswith(f"/agents/{name}") and card["security"], card
print("PASS A2A rejects missing/invalid credentials" + (" and JWTs without nationalteam:sre" if no_sre else "") + ("; SRE-group JWT accepted" if SRE_TOKEN else ""))

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
    ("tool-postgres", "postgres_postgres_query", {"sql": "SELECT 1 AS value"}, [{"value": 1}]),
]:
    token = TOKENS[identity]
    # Current gateway policy may filter denied tools in tools/list; it must deny
    # a direct call as well. Compare actual public names without leaking JWTs.
    visible = mcp_rpc("tools/list", {}, token, 10)["result"]["tools"]
    allowed_name = next(tool["name"] for tool in visible if tool["name"].endswith(allowed_suffix))
    result = mcp_call(allowed_name, args, token, 11)["result"]
    assert not result.get("isError"), result
    actual = json.loads(result["content"][0]["text"])
    if identity == "tool-postgres":
        assert actual == expected, result
    else:
        assert actual == expected, result
    denied_tools = ["playground_calculate", "playground_text_stats", "playground_reverse_text"]
    if identity != "tool-postgres":
        denied_tools.append("postgres_postgres_query")
    for denied_name in denied_tools:
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

# A valid JWT without a tool grant is denied by MCP.
no_access_paths = [("/mcp", {"jsonrpc": "2.0", "id": 20, "method": "tools/list", "params": {}})]
for path, body in no_access_paths:
    try:
        request(path, body, token=no_access)
    except HTTPError as error:
        assert error.code in (401, 403), (path, error.code)
    else:
        raise AssertionError(f"Valid identity without an access grant accepted for {path}")
print("PASS valid JWT without a tool claim is denied by MCP")



for name, text, expected in [
    ("calculator", "(12 + 8) * 3", {"value": 60}),
    ("text-analyst", "Hello from agentgateway", {"characters": 23, "words": 3, "lines": 1}),
    ("text-transformer", "agentgateway", {"text": "yawetagtnega"}),
    ("research", "https://www.iana.org/", None),
    ("data-analyst", json.dumps({"format": "csv", "data": "n\n1\n3\n"}), {"rows": 2}),
    ("issue-triage", "Deployment gateway crash", {"team": "platform"}),
]:
    path = "/agents/" + name
    card = request(path + "/.well-known/agent-card.json", token="", api_key=AGW_API_KEY)
    assert card["url"].endswith(path) and card["security"], card
    result = rpc(path, "message/send", {"message": {
        "kind": "message", "role": "user", "messageId": str(uuid4()),
        "parts": [{"kind": "text", "text": text}],
    }}, token="", api_key=AGW_API_KEY)
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
