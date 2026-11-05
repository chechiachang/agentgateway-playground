"""Exercise A2A over HTTP against a real fake app-server subprocess."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("coder", ROOT / "examples/codex-agent.py")
coder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(coder)


class AdapterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.backend = coder.Codex([sys.executable, "-B", str(ROOT / "tests/fixtures/fake-codex.py")], self.temp.name)
        self.agent = coder.Agent(self.backend, self.temp.name, "test-model", timeout=10)
        self.server = coder.ThreadingHTTPServer(("127.0.0.1", 0), coder.Handler)
        self.server.daemon_threads = True
        self.server.agent = self.agent
        self.server.token = "private-test-token-" * 3
        self.server.public_url = "http://localhost:4000"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.agent.close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, method, params, stream=False):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        request = Request(self.url + "/agents/coder", data=body, headers={"Content-Type": "application/json", "x-codex-agent-token": self.server.token})
        with urlopen(request, timeout=5) as response:
            if stream:
                return [json.loads(line[6:])["result"] for line in response if line.startswith(b"data: ")]
            return json.load(response)

    def submit(self, text, message_id="test", context=None):
        message = {"role": "user", "messageId": message_id, "parts": [{"kind": "text", "text": text}]}
        if context:
            message["contextId"] = context
        return {"message": message, "configuration": {"blocking": False}}

    def wait(self, task_id):
        task = self.agent.get({"id": task_id})
        with self.agent.condition:
            self.assertTrue(self.agent.condition.wait_for(lambda: task["status"]["state"] in coder.TERMINAL, timeout=5))
        return self.request("tasks/get", {"id": task_id})["result"]

    def test_auth_and_card(self):
        for path in ("/agents/coder", "/agents/coder/.well-known/agent-card.json"):
            with self.assertRaises(HTTPError) as failure:
                urlopen(self.url + path)
            self.assertEqual(failure.exception.code, 401)
            failure.exception.close()
        request = Request(self.url + "/agents/coder/.well-known/agent-card.json", headers={"x-codex-agent-token": self.server.token})
        with urlopen(request) as response:
            card = json.load(response)
        self.assertTrue(card["capabilities"]["streaming"])
        self.assertEqual(card["url"], "http://localhost:4000/agents/coder")

    def test_completion_idempotency_followup_and_failure(self):
        params = self.submit("hello")
        first = self.request("message/send", params)["result"]
        duplicate = self.request("message/send", params)["result"]
        self.assertEqual(first["id"], duplicate["id"])
        done = self.wait(first["id"])
        self.assertEqual(done["status"]["state"], "completed")
        self.assertEqual([a["name"] for a in done["artifacts"]], ["response", "diff"])
        self.assertEqual(done["artifacts"][0]["parts"][0]["text"], "done hello")
        following = self.request("message/send", self.submit("fail", "next", done["contextId"]))["result"]
        failed = self.wait(following["id"])
        self.assertEqual(failed["status"]["state"], "failed")
        self.assertEqual(len(self.agent.contexts), 1)
        self.assertEqual(self.agent.contexts[done["contextId"]], "thread-1")
        self.assertEqual(self.request("message/send", self.submit("different"))["error"]["code"], -32602)

    def test_stream_and_resubscribe(self):
        events = self.request("message/stream", self.submit("hello"), stream=True)
        self.assertEqual(events[0]["kind"], "task")
        self.assertTrue(events[-1]["final"])
        self.assertEqual(events[-1]["status"]["state"], "completed")
        self.assertTrue(any(e["kind"] == "artifact-update" for e in events))
        replay = self.request("tasks/resubscribe", {"id": events[0]["id"]}, stream=True)
        self.assertEqual(replay[0]["status"]["state"], "completed")

    def test_cancel_active_and_queued_without_running_queued_task(self):
        active = self.request("message/send", self.submit("slow"))["result"]
        deadline = time.monotonic() + 3
        while not self.agent.contexts[active["contextId"]] and time.monotonic() < deadline:
            time.sleep(.01)
        queued = self.request("message/send", self.submit("hello", "queued"))["result"]
        self.request("tasks/cancel", {"id": queued["id"]})
        self.request("tasks/cancel", {"id": active["id"]})
        self.assertEqual(self.wait(active["id"])["status"]["state"], "canceled")
        self.assertEqual(self.wait(queued["id"])["status"]["state"], "canceled")
        self.assertIsNone(self.agent.contexts[queued["contextId"]])
        self.assertEqual(self.request("tasks/cancel", {"id": active["id"]})["error"]["code"], -32002)

    def test_timeout_approval_and_process_exit(self):
        approved = self.request("message/send", self.submit("approval"))["result"]
        self.assertEqual(self.wait(approved["id"])["status"]["state"], "completed")
        self.agent.timeout = .1
        slow = self.request("message/send", self.submit("slow", "timeout"))["result"]
        self.assertEqual(self.wait(slow["id"])["status"]["state"], "failed")
        crash = self.request("message/send", self.submit("crash", "crash"))["result"]
        self.assertEqual(self.wait(crash["id"])["status"]["state"], "failed")
        self.assertIn("error", self.request("message/send", self.submit("hello", "after-crash")))

    def test_validation_and_queue_limit(self):
        for params in ({}, self.submit(""), self.submit("hello", context="unknown"), {"message": {"parts": []}}):
            self.assertEqual(self.request("message/send", params)["error"]["code"], -32602)
        self.assertEqual(self.request("tasks/get", {"id": "unknown"})["error"]["code"], -32001)
        self.assertEqual(self.request("unknown", {})["error"]["code"], -32601)
        self.agent.tasks = {str(i): {"_message_id": str(i)} for i in range(128)}
        self.assertEqual(self.request("message/send", self.submit("hello"))["error"]["code"], -32001)


if __name__ == "__main__":
    unittest.main()
