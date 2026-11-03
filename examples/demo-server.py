#!/usr/bin/env python3
"""Dependency-free MCP tools and deterministic A2A demo agents (no model quota)."""
import ast
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import csv
import io
import ipaddress
import math
import operator
import os
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

BASE_URL = os.environ.get("DEMO_PUBLIC_URL", "http://localhost:4000").rstrip("/")
RESEARCH_ALLOWLIST = frozenset({"www.iana.org", "www.rfc-editor.org", "www.python.org"})
MAX_RESEARCH_BYTES = 65536
MAX_DATA_BYTES = 65536
MAX_DATA_ROWS = 1000
MAX_DATA_COLUMNS = 50
MAX_FIELD_CHARS = 4096
TRIAGE_TEAMS = ("platform", "security", "documentation", "product")
OPERATORS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
             ast.Div: operator.truediv, ast.Mod: operator.mod}
TOOLS = [
    {"name": "calculate", "description": "Evaluate arithmetic (+, -, *, /, %, parentheses).",
     "inputSchema": {"type": "object", "properties": {"expression": {"type": "string", "maxLength": 200}},
                     "required": ["expression"], "additionalProperties": False}},
    {"name": "text_stats", "description": "Count characters, words, and lines in text.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string", "maxLength": 10000}},
                     "required": ["text"], "additionalProperties": False}},
    {"name": "reverse_text", "description": "Reverse text by Unicode code point.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string", "maxLength": 10000}},
                     "required": ["text"], "additionalProperties": False}},
    {"name": "research", "description": "Fetch text over HTTPS from the fixed allowlist only; redirects disabled.",
     "inputSchema": {"type": "object", "properties": {"url": {"type": "string", "maxLength": 2048}},
                     "required": ["url"], "additionalProperties": False}},
    {"name": "data_summary", "description": "Summarize bounded CSV or JSON records with column statistics.",
     "inputSchema": {"type": "object", "properties": {"data": {"type": "string", "maxLength": 65536}, "format": {"type": "string", "enum": ["csv", "json"]}},
                     "required": ["data", "format"], "additionalProperties": False}},
    {"name": "issue_triage", "description": "Heuristic team suggestion only; does not authorize or perform actions.",
     "inputSchema": {"type": "object", "properties": {"issue": {"type": "string", "maxLength": 10000}},
                     "required": ["issue"], "additionalProperties": False}},
]
AGENTS = {
    "calculator": {"name": "Calculator", "description": "Evaluates arithmetic without an LLM.",
                   "examples": ["(12 + 8) * 3"], "tags": ["math", "tools"]},
    "text-analyst": {"name": "Text analyst", "description": "Counts text characters, words, and lines without an LLM.",
                     "examples": ["Hello from agentgateway"], "tags": ["text", "tools"]},
    "text-transformer": {"name": "Text transformer", "description": "Reverses text by Unicode code point without an LLM.",
                          "examples": ["agentgateway"], "tags": ["text", "transform"]},
    "research": {"name": "Public-source researcher", "description": "Fetches short public pages only from a fixed HTTPS hostname allowlist; never crawls or follows redirects.",
                 "examples": ["https://www.iana.org/domains/reserved"], "tags": ["research", "https"]},
    "data-analyst": {"name": "Data analyst", "description": "Summarizes bounded CSV or JSON records deterministically without an LLM.",
                     "examples": ["Analyze a small CSV table"], "tags": ["data", "statistics"]},
    "issue-triage": {"name": "Issue triage", "description": "Heuristically suggests a fixed team with rationale and confidence; never authorizes or performs actions.",
                     "examples": ["Login fails after deployment"], "tags": ["triage", "heuristic"]},
}


def calculate(expression):
    if not isinstance(expression, str) or not expression.strip() or len(expression) > 200:
        raise ValueError("expression must be a nonempty string of at most 200 characters")

    def evaluate(node):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            value = node.value
        elif isinstance(node, ast.BinOp) and type(node.op) in OPERATORS:
            value = OPERATORS[type(node.op)](evaluate(node.left), evaluate(node.right))
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = evaluate(node.operand) * (-1 if isinstance(node.op, ast.USub) else 1)
        else:
            raise ValueError("Only numbers and +, -, *, /, %, parentheses are supported")
        if not math.isfinite(value) or abs(value) > 1e100:
            raise ValueError("Result is outside the supported numeric range")
        return value

    return {"value": evaluate(ast.parse(expression.strip(), mode="eval").body)}


def text_stats(text):
    if not isinstance(text, str) or len(text) > 10000:
        raise ValueError("text must be a string of at most 10000 characters")
    return {"characters": len(text), "words": len(text.split()), "lines": len(text.splitlines())}


def reverse_text(text):
    if not isinstance(text, str) or len(text) > 10000:
        raise ValueError("text must be a string of at most 10000 characters")
    return {"text": text[::-1]}



def validate_research_url(url):
    if not isinstance(url, str) or len(url) > 2048:
        raise ValueError("url must be a string of at most 2048 characters")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise ValueError("invalid URL") from error
    if (parsed.scheme != "https" or not host or host.lower() not in RESEARCH_ALLOWLIST
            or parsed.username is not None or parsed.password is not None
            or port not in (None, 443) or parsed.fragment):
        raise ValueError("URL must be HTTPS on an explicitly allowed public hostname (default port only)")
    # Reject hostnames resolving to any non-global address, including mixed public/private answers.
    try:
        addresses = {entry[4][0] for entry in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    except OSError as error:
        raise ValueError("hostname could not be resolved") from error
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("hostname resolves to a non-public address")
    return url


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def research(url):
    url = validate_research_url(url)
    opener = build_opener(NoRedirect)
    request = Request(url, headers={"User-Agent": "agentgateway-playground-research/1.0", "Accept": "text/plain, text/html"})
    try:
        response = opener.open(request, timeout=3)
    except HTTPError as error:
        if error.code in (301, 302, 303, 307, 308):
            raise ValueError("redirects are not followed") from error
        raise ValueError("public source returned HTTP error") from error
    except (URLError, OSError, TimeoutError) as error:
        raise ValueError("public source could not be fetched") from error
    with response:
        final = response.geturl()
        if validate_research_url(final) != url:
            raise ValueError("unexpected response URL")
        content_type = response.headers.get_content_type()
        if content_type not in ("text/plain", "text/html"):
            raise ValueError("only text/plain and text/html responses are supported")
        length = response.headers.get("Content-Length")
        if length and (not length.isdecimal() or int(length) > MAX_RESEARCH_BYTES):
            raise ValueError("response exceeds 65536 bytes")
        status = response.status
        try:
            data = response.read(MAX_RESEARCH_BYTES + 1)
        except (OSError, TimeoutError) as error:
            raise ValueError("public source could not be fetched") from error
    if len(data) > MAX_RESEARCH_BYTES:
        raise ValueError("response exceeds 65536 bytes")
    text = data.decode("utf-8", errors="replace")
    return {"url": url, "status": status, "content_type": content_type,
            "bytes": len(data), "text": text[:16000]}


def data_summary(data, fmt):
    if not isinstance(data, str):
        raise ValueError("data must be a string")
    try:
        size = len(data.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise ValueError("data must be valid UTF-8 text") from error
    if size > MAX_DATA_BYTES:
        raise ValueError("data must be a string of at most 65536 UTF-8 bytes")
    if fmt not in ("csv", "json"):
        raise ValueError("format must be csv or json")
    try:
        if fmt == "csv":
            reader = csv.DictReader(io.StringIO(data, newline=""))
            headers = reader.fieldnames
            if not headers or len(headers) > MAX_DATA_COLUMNS or any(not h or len(h) > 128 for h in headers) or len(set(headers)) != len(headers):
                raise ValueError("CSV requires 1-50 unique, nonempty column names")
            rows = list(reader)
            if any(None in row or any(not isinstance(v, str) or len(v) > MAX_FIELD_CHARS for v in row.values()) for row in rows):
                raise ValueError("CSV rows have invalid columns or oversized fields")
        else:
            parsed = json.loads(data)
            if not isinstance(parsed, list) or any(not isinstance(row, dict) for row in parsed):
                raise ValueError("JSON must be an array of objects")
            rows = parsed
            headers = list(dict.fromkeys(key for row in rows for key in row))
            if not headers or len(headers) > MAX_DATA_COLUMNS:
                raise ValueError("JSON requires 1-50 columns")
            if any(not isinstance(k, str) or len(k) > 128 or len(json.dumps(v, ensure_ascii=False)) > MAX_FIELD_CHARS
                   for row in rows for k, v in row.items()):
                raise ValueError("JSON keys and values must be bounded")
        if len(rows) > MAX_DATA_ROWS:
            raise ValueError("data may contain at most 1000 rows")
        if any(set(row) - set(headers) for row in rows):
            raise ValueError("rows contain inconsistent columns")
    except (csv.Error, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("invalid CSV or JSON input") from error
    result = {"rows": len(rows), "columns": headers, "column_summaries": {}}
    for column in headers:
        values = [row.get(column) for row in rows if row.get(column) is not None and row.get(column) != ""]
        nums = []
        for value in values:
            if isinstance(value, bool) or isinstance(value, (dict, list)):
                nums = []
                break
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError):
                nums = []
                break
            if not math.isfinite(number):
                nums = []
                break
            nums.append(number)
        summary = {"non_null": len(values), "missing": len(rows) - len(values), "distinct": len({json.dumps(v, sort_keys=True) for v in values})}
        if values and len(nums) == len(values):
            ordered = sorted(nums)
            scale = max(abs(number) for number in nums)
            if scale:
                scaled_mean = math.fsum(number / scale for number in nums) / len(nums)
                scaled_mean = min(max(scaled_mean, min(nums) / scale), max(nums) / scale)
                mean = scale * scaled_mean
            else:
                mean = 0.0
            middle = len(ordered) // 2
            if len(ordered) % 2:
                median = ordered[middle]
            else:
                lower, upper = ordered[middle - 1], ordered[middle]
                median = (lower + upper) / 2 if lower < 0 < upper else lower + (upper - lower) / 2
            summary.update({"type": "numeric", "min": min(nums), "max": max(nums),
                           "mean": mean, "median": median})
        else:
            summary["type"] = "categorical"
            grouped = {}
            for value in values:
                key = json.dumps(value, sort_keys=True, ensure_ascii=False)
                if key not in grouped:
                    grouped[key] = [str(value), 0]
                grouped[key][1] += 1
            summary["top_values"] = sorted((tuple(item) for item in grouped.values()), key=lambda pair: (-pair[1], pair[0]))[:5]
        result["column_summaries"][column] = summary
    return result


TRIAGE_RULES = {
    "security": ("security", "vulnerability", "exploit", "cve", "secret", "credential", "permission", "auth"),
    "platform": ("deploy", "deployment", "gateway", "docker", "compose", "build", "ci", "runtime", "crash", "performance"),
    "documentation": ("docs", "documentation", "readme", "typo", "example", "guide"),
    "product": ("feature", "request", "ux", "interface", "workflow"),
}


def triage_issue(text):
    if not isinstance(text, str) or not text.strip() or len(text) > 10000:
        raise ValueError("issue must be a nonempty string of at most 10000 characters")
    normalized = text.casefold()
    scores = {team: sum(1 for term in terms if term in normalized) for team, terms in TRIAGE_RULES.items()}
    team = max(TRIAGE_TEAMS, key=lambda candidate: scores[candidate])
    winning = scores[team]
    if winning == 0:
        team, confidence, rationale = "product", 0.25, "No routing keywords matched; product is the deterministic fallback."
    else:
        confidence = min(0.9, 0.5 + 0.1 * winning)
        rationale = "Matched heuristic keywords: " + ", ".join(term for term in TRIAGE_RULES[team] if term in normalized) + "."
    return {"team": team, "rationale": rationale, "confidence": confidence,
            "heuristic": True, "note": "Suggestion only; not authorization, assignment, or an action."}


def call_tool(name, arguments):
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    if name == "calculate" and set(arguments) == {"expression"}:
        return calculate(arguments["expression"])
    if name == "text_stats" and set(arguments) == {"text"}:
        return text_stats(arguments["text"])
    if name == "reverse_text" and set(arguments) == {"text"}:
        return reverse_text(arguments["text"])
    if name == "research" and set(arguments) == {"url"}:
        return research(arguments["url"])
    if name == "data_summary" and set(arguments) == {"data", "format"}:
        return data_summary(arguments["data"], arguments["format"])
    if name == "issue_triage" and set(arguments) == {"issue"}:
        return triage_issue(arguments["issue"])
    raise ValueError("Unknown tool or invalid arguments")


def agent_card(name):
    agent = AGENTS[name]
    return {"protocolVersion": "0.3.0", "name": agent["name"], "description": agent["description"],
            "url": f"{BASE_URL}/agents/{name}", "version": "1.0.0", "preferredTransport": "JSONRPC",
            "capabilities": {"streaming": False, "pushNotifications": False},
            "defaultInputModes": ["text/plain"], "defaultOutputModes": ["text/plain"],
            "securitySchemes": {"vaultJwt": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}},
            "security": [{"vaultJwt": []}],
            "skills": [{"id": name, **agent, "inputModes": ["text/plain"], "outputModes": ["text/plain"]}]}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass  # Never log request bodies or bearer tokens.

    def reply(self, status, body=None):
        data = b"" if body is None else json.dumps(body, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            self.reply(200, {"status": "ok"})
            return
        for name in AGENTS:
            if self.path in (f"/agents/{name}/.well-known/agent-card.json",
                             f"/agents/{name}/.well-known/agent.json"):
                self.reply(200, agent_card(name))
                return
        self.reply(405 if self.path == "/mcp" else 404)

    def do_DELETE(self):
        self.reply(405)  # Stateless MCP: no sessions to terminate.

    def do_POST(self):
        agent = self.path.removeprefix("/agents/")
        if self.path != "/mcp" and agent not in AGENTS:
            self.reply(404)
            return
        request_id = None
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                self.close_connection = True
                self.reply(413)
                return
            try:
                request = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeDecodeError):
                self.rpc_error(None, -32700, "Parse error")
                return
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
                self.rpc_error(None, -32600, "Invalid Request")
                return
            request_id = request.get("id")
            method = request["method"]
            if "id" not in request:
                self.reply(202)
                return
            params = request.get("params", {})
            if not isinstance(params, dict):
                raise ValueError("params must be an object")
            if self.path == "/mcp":
                result = self.mcp(method, params)
            else:
                result = self.a2a(agent, method, params)
            if result is None:
                self.rpc_error(request_id, -32601, "Method not found")
            else:
                self.reply(200, {"jsonrpc": "2.0", "id": request_id, "result": result})
        except (ValueError, SyntaxError, ArithmeticError, RecursionError) as error:
            self.rpc_error(request_id, -32602, str(error))

    def rpc_error(self, request_id, code, message):
        self.reply(200, {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

    def mcp(self, method, params):
        if method == "initialize":
            version = params.get("protocolVersion")
            return {"protocolVersion": version if version in ("2024-11-05", "2025-03-26", "2025-06-18") else "2025-06-18",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "playground-tools", "version": "1.0.0"}}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            try:
                value = call_tool(params.get("name"), params.get("arguments", {}))
                return {"content": [{"type": "text", "text": json.dumps(value)}], "isError": False}
            except (ValueError, SyntaxError, ArithmeticError, RecursionError) as error:
                return {"content": [{"type": "text", "text": str(error)}], "isError": True}
        return None

    def a2a(self, agent, method, params):
        if method != "message/send":
            return None
        message = params.get("message")
        if not isinstance(message, dict) or message.get("role") != "user" or not isinstance(message.get("messageId"), str):
            raise ValueError("message requires role=user and a messageId")
        parts = message.get("parts")
        if not isinstance(parts, list) or not parts or any(
            not isinstance(part, dict) or part.get("kind") != "text" or not isinstance(part.get("text"), str)
            for part in parts
        ):
            raise ValueError("message.parts must contain text parts")
        text = "\n".join(part["text"] for part in parts)
        try:
            text_size = len(text.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise ValueError("message must contain valid UTF-8 text") from error
        if text_size > 65536:
            raise ValueError("message exceeds 65536 UTF-8 bytes")
        if agent == "calculator":
            value = calculate(text)
        elif agent == "text-analyst":
            value = text_stats(text)
        elif agent == "text-transformer":
            value = reverse_text(text)
        elif agent == "research":
            value = research(text)
        elif agent == "data-analyst":
            try:
                payload = json.loads(text)
            except (ValueError, RecursionError):
                raise ValueError("data-analyst message must be JSON with format and data")
            if not isinstance(payload, dict) or set(payload) != {"format", "data"}:
                raise ValueError("data-analyst message must contain exactly format and data")
            raw = payload["data"]
            if not isinstance(raw, str):
                raw = json.dumps(raw, ensure_ascii=False, allow_nan=False)
            value = data_summary(raw, payload["format"])
        elif agent == "issue-triage":
            value = triage_issue(text)
        else:
            raise ValueError("Unknown agent")
        return {"kind": "message", "role": "agent", "messageId": str(uuid4()),
                "parts": [{"kind": "text", "text": json.dumps(value)}]}


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
