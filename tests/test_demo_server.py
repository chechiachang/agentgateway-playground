"""Check tool validation and MCP/A2A wire behavior without Docker or credentials."""
import importlib.util
import json
from pathlib import Path
from threading import Thread
import unittest
from urllib.request import Request, urlopen

spec = importlib.util.spec_from_file_location("demo", Path(__file__).resolve().parents[1] / "examples/demo-server.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


class ToolsTest(unittest.TestCase):
    def test_arithmetic(self):
        self.assertEqual(demo.calculate("-(12 + 8) * 3 / 2"), {"value": -30})
        self.assertEqual(demo.calculate("10 % 3"), {"value": 1})

    def test_rejects_code_and_unbounded_math(self):
        for expression in ("__import__('os')", "1 / 0", "2 ** 10000", "True", "[1]", "1e999", "9" * 201):
            with self.subTest(expression=expression), self.assertRaises((ValueError, ArithmeticError)):
                demo.calculate(expression)

    def test_text_and_argument_validation(self):
        self.assertEqual(demo.text_stats("one two\nthree"), {"characters": 13, "words": 3, "lines": 2})
        self.assertEqual(demo.text_stats(""), {"characters": 0, "words": 0, "lines": 0})
        self.assertEqual(demo.reverse_text("héllo 🌍"), {"text": "🌍 olléh"})
        for name, arguments in (("unknown", {}), ("calculate", {}), ("text_stats", {"text": 12}),
                                ("text_stats", {"text": "x" * 10001}), ("calculate", {"expression": "1", "extra": 2})):
            with self.subTest(name=name), self.assertRaises(ValueError):
                demo.call_tool(name, arguments)


class ProtocolTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = demo.ThreadingHTTPServer(("127.0.0.1", 0), demo.Handler)
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def request(self, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        with urlopen(Request(self.base + path, data=data, headers={"Content-Type": "application/json"}), timeout=3) as response:
            data = response.read()
            return response.status, json.loads(data) if data else None

    def rpc(self, path, method, params):
        return self.request(path, {"jsonrpc": "2.0", "id": 7, "method": method, "params": params})[1]

    def test_mcp_handshake_and_tool_errors(self):
        result = self.rpc("/mcp", "initialize", {"protocolVersion": "2025-06-18"})
        self.assertEqual(result["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(self.request("/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"}), (202, None))
        self.assertEqual(len(self.rpc("/mcp", "tools/list", {})["result"]["tools"]), 6)
        result = self.rpc("/mcp", "tools/call", {"name": "calculate", "arguments": {"expression": "1/0"}})
        self.assertTrue(result["result"]["isError"])
        self.assertEqual(self.rpc("/mcp", "unknown", {})["error"]["code"], -32601)
        self.assertEqual(self.rpc("/mcp", "tools/list", [])["error"]["code"], -32602)

    def test_agent_discovery_and_messages(self):
        for name, text, expected in (("calculator", "6 * 7", {"value": 42}),
                                     ("text-analyst", "one two", {"characters": 7, "words": 2, "lines": 1}),
                                     ("text-transformer", "héllo 🌍", {"text": "🌍 olléh"})):
            path = "/agents/" + name
            card = self.request(path + "/.well-known/agent-card.json")[1]
            self.assertEqual(card["url"], demo.BASE_URL + path)
            self.assertFalse(card["capabilities"]["streaming"])
            self.assertTrue(card["security"])
            result = self.rpc(path, "message/send", {"message": {
                "role": "user", "messageId": "test", "parts": [{"kind": "text", "text": text}]}})["result"]
            self.assertEqual(json.loads(result["parts"][0]["text"]), expected)
            self.assertEqual(self.rpc(path, "message/send", {"message": {}})["error"]["code"], -32602)

    def test_invalid_jsonrpc(self):
        self.assertEqual(self.request("/mcp", [1, 2])[1]["error"]["code"], -32600)
        self.assertEqual(self.request("/mcp", {"method": "ping"})[1]["error"]["code"], -32600)


if __name__ == "__main__":
    unittest.main()
