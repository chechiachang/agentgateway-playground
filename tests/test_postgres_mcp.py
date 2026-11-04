import importlib.util
from unittest.mock import MagicMock, patch
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("postgres_mcp", Path(__file__).resolve().parents[1] / "examples/postgres-mcp.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class ReadOnlySqlTests(unittest.TestCase):
    def test_accepts_select(self):
        self.assertEqual(server.readonly_query(" SELECT id FROM public.items "), "SELECT id FROM public.items")

    def test_rejects_mutation_and_multiple_statements(self):
        for query in ("DELETE FROM items", "SELECT 1; DROP TABLE items", "SELECT 1 --comment", "SELECT 1 INTO other", "SELECT * FROM items FOR UPDATE", "SELECT pg_sleep(20)"):
            with self.subTest(query=query), self.assertRaises(ValueError):
                server.readonly_query(query)

    def test_rejects_quoted_and_unicode_escaped_unsafe_functions(self):
        for query in (
            'SELECT "pg_terminate_backend"(123)',
            'SELECT pg_catalog."pg_terminate_backend"(123)',
            'SELECT U&"pg\\005fterminate_backend"(123)',
        ):
            with self.subTest(query=query), self.assertRaises(ValueError):
                server.readonly_query(query)

    def test_ignores_function_like_text_in_string_literals(self):
        self.assertEqual(server.readonly_query("SELECT 'pg_terminate_backend('"), "SELECT 'pg_terminate_backend('")

    def test_query_length(self):
        with self.assertRaises(ValueError):
            server.readonly_query("SELECT " + " " * server.MAX_SQL_CHARS)

    def test_unknown_tool_rejected(self):
        with self.assertRaises(ValueError):
            server.call_tool("not-a-tool", {})

    def test_query_uses_supported_read_only_transaction_and_preserves_percent_signs(self):
        for query in ("SELECT 10 % 3 AS value", "SELECT 'value' LIKE '%alue' AS matches"):
            with self.subTest(query=query):
                conn = MagicMock()
                driver = MagicMock()
                driver.connect.return_value.__enter__.return_value = conn
                with patch.object(server, "psycopg", driver), patch.dict("os.environ", {"DATABASE_URL": "postgres://test"}):
                    server.call_tool("postgres_query", {"sql": query})
                conn.transaction.assert_called_once_with()
                conn.execute.assert_called_once_with(
                    f"SELECT * FROM ({query}) AS mcp_read_only_query LIMIT {server.MAX_ROWS}"
                )


if __name__ == "__main__":
    unittest.main()
