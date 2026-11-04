#!/usr/bin/env python3
"""Small read-only PostgreSQL MCP server; stdlib transport, psycopg driver."""
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # SQL validation tests can run without the container driver.
    psycopg = None
    dict_row = None

MAX_ROWS = 200
MAX_SQL_CHARS = 8000
FORBIDDEN = re.compile(r"\b(insert|update|delete|merge|create|alter|drop|truncate|grant|revoke|copy|call|do|vacuum|refresh|comment|set|reset|discard|listen|notify|prepare|execute|deallocate)\b", re.I)
UNSAFE_FUNCTIONS = {"pg_sleep", "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "lo_import", "lo_export", "dblink"}
TOOLS = [
    {"name": "postgres_list_tables", "description": "List accessible tables in the public schema.", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "postgres_describe_table", "description": "Describe columns for a public-schema table.", "inputSchema": {"type": "object", "properties": {"table": {"type": "string", "maxLength": 63}}, "required": ["table"], "additionalProperties": False}},
    {"name": "postgres_query", "description": "Run one bounded, read-only SELECT query (maximum 200 rows).", "inputSchema": {"type": "object", "properties": {"sql": {"type": "string", "maxLength": MAX_SQL_CHARS}}, "required": ["sql"], "additionalProperties": False}},
]


def _sql_tokens(query):
    """Tokenize identifiers and statement punctuation without interpreting string contents."""
    tokens = []
    index = 0
    while index < len(query):
        char = query[index]
        if char.isspace():
            index += 1
        elif query.startswith(("--", "/*"), index):
            raise ValueError("SQL comments are not allowed")
        elif char == "'":
            escaped_string = index > 0 and query[index - 1] in "eE" and (index < 2 or not (query[index - 2].isalnum() or query[index - 2] in "_$"))
            index += 1
            while index < len(query):
                if escaped_string and query[index] == "\\":
                    index += 2
                elif query[index] == "'" and index + 1 < len(query) and query[index + 1] == "'":
                    index += 2
                elif query[index] == "'":
                    index += 1
                    break
                else:
                    index += 1
            tokens.append(("literal", ""))
        elif query.startswith("$", index):
            delimiter = re.match(r"\$(?:[a-zA-Z_][a-zA-Z0-9_]*)?\$", query[index:])
            if delimiter:
                marker = delimiter.group()
                end = query.find(marker, index + len(marker))
                if end >= 0:
                    index = end + len(marker)
                    tokens.append(("literal", ""))
                    continue
            tokens.append(("symbol", char))
            index += 1
        elif char == '"' or query[index:index + 3].lower() == 'u&"':
            unicode_identifier = char != '"'
            index += 3 if unicode_identifier else 1
            identifier = []
            while index < len(query):
                if query[index] == '"' and index + 1 < len(query) and query[index + 1] == '"':
                    identifier.append('"')
                    index += 2
                elif query[index] == '"':
                    index += 1
                    break
                else:
                    identifier.append(query[index])
                    index += 1
            identifier = "".join(identifier)
            if unicode_identifier:
                escape = "\\"
                clause_start = index
                while clause_start < len(query) and query[clause_start].isspace():
                    clause_start += 1
                clause_end = clause_start + len("UESCAPE")
                if query[clause_start:clause_end].upper() == "UESCAPE" and (clause_end == len(query) or not (query[clause_end].isalnum() or query[clause_end] in "_$")):
                    value_start = clause_end
                    while value_start < len(query) and query[value_start].isspace():
                        value_start += 1
                    if value_start < len(query) and query[value_start] == "'":
                        value_start += 1
                        value = []
                        while value_start < len(query):
                            if query[value_start] == "'" and value_start + 1 < len(query) and query[value_start + 1] == "'":
                                value.append("'")
                                value_start += 2
                            elif query[value_start] == "'":
                                value_start += 1
                                break
                            else:
                                value.append(query[value_start])
                                value_start += 1
                        if len(value) == 1:
                            escape = value[0]
                            index = value_start
                decoded = []
                pos = 0
                while pos < len(identifier):
                    if identifier[pos] != escape or pos + 1 >= len(identifier):
                        decoded.append(identifier[pos])
                        pos += 1
                    elif identifier[pos + 1] == escape:
                        decoded.append(escape)
                        pos += 2
                    else:
                        start = pos + 2 if identifier[pos + 1] == "+" else pos + 1
                        digits = 6 if start != pos + 1 else 4
                        codepoint = identifier[start:start + digits]
                        try:
                            decoded.append(chr(int(codepoint, 16)))
                            pos = start + digits
                        except (ValueError, OverflowError):
                            decoded.extend(identifier[pos:pos + 2])
                            pos += 2
                identifier = "".join(decoded)
            tokens.append(("identifier", identifier.lower()))
        elif char.isalpha() or char == "_" or ord(char) >= 128:
            end = index + 1
            while end < len(query) and (query[end].isalnum() or query[end] in "_$" or ord(query[end]) >= 128):
                end += 1
            tokens.append(("identifier", query[index:end].lower()))
            index = end
        else:
            if char == ";":
                raise ValueError("only a single SELECT statement is allowed")
            tokens.append(("symbol", char))
            index += 1
    return tokens


def readonly_query(sql):
    """Conservative syntax checks plus PostgreSQL read-only transaction and DB grants."""
    if not isinstance(sql, str) or len(sql) > MAX_SQL_CHARS:
        raise ValueError("sql must be a string no longer than 8000 characters")
    query = sql.strip()
    tokens = _sql_tokens(query)
    if not re.match(r"(?is)^select\b", query) or FORBIDDEN.search(query):
        raise ValueError("only a single SELECT statement is allowed")
    if any(kind == "identifier" and (value.startswith("pg_") or value in UNSAFE_FUNCTIONS) and index + 1 < len(tokens) and tokens[index + 1] == ("symbol", "(") for index, (kind, value) in enumerate(tokens)):
        raise ValueError("unsafe PostgreSQL function is not allowed")
    if re.search(r"(?is)\b(into|for\s+(update|share|no\s+key\s+update|key\s+share))\b", query):
        raise ValueError("SELECT INTO and row-locking clauses are not allowed")
    return query


def call_tool(name, args):
    if name not in {"postgres_list_tables", "postgres_describe_table", "postgres_query"}:
        raise ValueError("unknown tool")
    if psycopg is None:
        raise RuntimeError("psycopg driver is not installed")
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=5, options="-c default_transaction_read_only=on -c statement_timeout=5000 -c lock_timeout=2000", row_factory=dict_row) as conn:
        with conn.transaction():
            if name == "postgres_list_tables":
                rows = conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY table_name").fetchall()
            elif name == "postgres_describe_table":
                table = args.get("table")
                if not isinstance(table, str) or len(table) > 63:
                    raise ValueError("table must be a PostgreSQL identifier")
                rows = conn.execute("SELECT column_name, data_type, is_nullable FROM information_schema.columns WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position", (table,)).fetchall()
            elif name == "postgres_query":
                query = readonly_query(args.get("sql"))
                rows = conn.execute("SELECT * FROM (" + query + f") AS mcp_read_only_query LIMIT {MAX_ROWS}").fetchall()
            else:
                raise ValueError("unknown tool")
    return rows


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def respond(self, status, value):
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.respond(200, {"ok": True}) if self.path == "/health" else self.respond(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/mcp":
            self.respond(404, {"error": "not found"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size < 0 or size > 65536:
                raise ValueError("request too large")
            request = json.loads(self.rfile.read(size))
            method, request_id = request.get("method"), request.get("id")
            if method == "initialize":
                result = {"protocolVersion": request.get("params", {}).get("protocolVersion", "2025-06-18"), "capabilities": {"tools": {}}, "serverInfo": {"name": "postgres-readonly", "version": "1.0.0"}}
            elif method == "notifications/initialized":
                self.send_response(202); self.end_headers(); return
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                params = request.get("params", {})
                try:
                    rows = call_tool(params.get("name"), params.get("arguments", {}))
                    result = {"content": [{"type": "text", "text": json.dumps(rows, default=str)}], "isError": False}
                except Exception as exc:
                    result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
            else:
                self.respond(200, {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "method not found"}}); return
            if request_id is None:
                self.send_response(202); self.end_headers(); return
            self.respond(200, {"jsonrpc": "2.0", "id": request_id, "result": result})
        except Exception:
            self.respond(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "invalid request"}})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8081), Handler).serve_forever()
