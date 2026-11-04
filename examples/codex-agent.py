#!/usr/bin/env python3
"""A2A 0.3 adapter for a long-lived local Codex app-server (stdlib only)."""
import argparse
import copy
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
from uuid import uuid4

TERMINAL = {"completed", "failed", "canceled", "rejected"}
MAX_OUTPUT = 262144


class RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Codex:
    """Single reader dispatches responses separately from agent notifications."""
    def __init__(self, command, cwd):
        self.pending = {}
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.events = queue.Queue(maxsize=4096)
        self.sequence = 0
        self.proc = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=None,
                                     text=True, encoding="utf-8", bufsize=1)
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()
        try:
            self.call("initialize", {"clientInfo": {"name": "agw_coder", "version": "1.0.0"}})
            self.send({"method": "initialized", "params": {}})
        except Exception:
            self.close()
            raise

    def send(self, message):
        with self.write_lock:
            self.proc.stdin.write(json.dumps(message, allow_nan=False) + "\n")
            self.proc.stdin.flush()

    def call(self, method, params, timeout=20):
        response = queue.Queue(maxsize=1)
        with self.lock:
            self.sequence += 1
            request_id = self.sequence
            self.pending[request_id] = response
        try:
            if self.proc.poll() is not None:
                raise RuntimeError("Codex app-server stopped; restart the adapter")
            self.send({"id": request_id, "method": method, "params": params})
            try:
                message = response.get(timeout=timeout)
            except queue.Empty:
                # An ambiguous RPC must not leave background work in this workspace.
                self.close()
                raise RuntimeError("Codex RPC timed out; restart the adapter") from None
            if "error" in message:
                raise RuntimeError(message["error"]["message"])
            return message["result"]
        finally:
            with self.lock:
                self.pending.pop(request_id, None)

    def read(self):
        try:
            for line in self.proc.stdout:
                message = json.loads(line)
                if "method" not in message:
                    with self.lock:
                        target = self.pending.get(message.get("id"))
                    if target:
                        target.put_nowait(message)
                    continue
                if "id" in message:
                    # No remote approval interface: never grant extra privileges.
                    method = message["method"]
                    if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
                        reply = {"result": {"decision": "decline"}}
                    elif method == "item/permissions/requestApproval":
                        reply = {"result": {"permissions": {}, "scope": "turn"}}
                    else:
                        reply = {"error": {"code": -32601, "message": "Interactive requests are not supported"}}
                    self.send({"id": message["id"], **reply})
                self.events.put_nowait(message)
        except (ValueError, OSError, queue.Full):
            self.proc.terminate()
        finally:
            with self.lock:
                for target in self.pending.values():
                    try:
                        target.put_nowait({"error": {"message": "Codex app-server disconnected"}})
                    except queue.Full:
                        pass

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.reader.join(timeout=2)
        self.proc.stdin.close()
        self.proc.stdout.close()


class Agent:
    """One bounded queue serializes all changes to a fixed workspace."""
    def __init__(self, codex, cwd, model, timeout=3600):
        self.codex, self.cwd, self.model, self.timeout = codex, str(Path(cwd).resolve()), model, timeout
        self.condition = threading.Condition()
        self.tasks, self.contexts = {}, {}
        self.work = queue.Queue(maxsize=16)
        self.stopping = threading.Event()
        self.worker = threading.Thread(target=self.run, daemon=True)
        self.worker.start()

    def snapshot(self, task):
        return copy.deepcopy({key: value for key, value in task.items() if not key.startswith("_")})

    def update(self, task, state=None, text=None):
        with self.condition:
            if state:
                task["status"] = {"state": state}
            if text:
                task["status"]["message"] = {"kind": "message", "role": "agent",
                    "messageId": str(uuid4()), "parts": [{"kind": "text", "text": text[:MAX_OUTPUT]}]}
            task["_version"] += 1
            self.condition.notify_all()

    def submit(self, params):
        message = params.get("message")
        if not isinstance(message, dict) or message.get("role") != "user" or not isinstance(message.get("messageId"), str) or not message["messageId"]:
            raise RpcError(-32602, "message requires role=user and a nonempty messageId")
        parts = message.get("parts")
        if not isinstance(parts, list) or not parts or any(not isinstance(p, dict) or p.get("kind") != "text" or not isinstance(p.get("text"), str) for p in parts):
            raise RpcError(-32602, "message.parts must contain text parts")
        text = "\n".join(p["text"] for p in parts)
        if not text.strip() or len(text.encode("utf-8")) > 65536:
            raise RpcError(-32602, "task must contain 1-65536 UTF-8 bytes")
        with self.condition:
            # Retry of a retained message must not run its changes twice.
            for task in self.tasks.values():
                if task["_message_id"] == message["messageId"]:
                    if task["_text"] != text or message.get("contextId") not in (None, task["contextId"]):
                        raise RpcError(-32602, "messageId already used with different input")
                    return task
            if message.get("taskId") is not None:
                raise RpcError(-32602, "use contextId, not taskId, for a follow-up")
            context = message.get("contextId")
            if context is not None and (not isinstance(context, str) or context not in self.contexts):
                raise RpcError(-32602, "unknown contextId (adapter state resets on restart)")
            if self.work.full() or len(self.tasks) >= 128 or (context is None and len(self.contexts) >= 128):
                raise RpcError(-32001, "task capacity reached; restart when idle or retry later")
            if self.codex.proc.poll() is not None:
                raise RpcError(-32000, "Codex stopped; restart the adapter")
            context = context or str(uuid4())
            self.contexts.setdefault(context, None)
            task = {"kind": "task", "id": str(uuid4()), "contextId": context,
                    "status": {"state": "submitted"}, "artifacts": [],
                    "_text": text, "_message_id": message["messageId"],
                    "_cancel": False, "_version": 0}
            self.tasks[task["id"]] = task
            self.work.put_nowait(task)
            return task

    def get(self, params):
        task_id = params.get("id")
        if not isinstance(task_id, str):
            raise RpcError(-32602, "id must be a task ID")
        with self.condition:
            if task_id not in self.tasks:
                raise RpcError(-32001, "Task not found")
            return self.tasks[task_id]

    def cancel(self, params):
        task = self.get(params)
        with self.condition:
            if task["status"]["state"] in TERMINAL:
                raise RpcError(-32002, "Task is already terminal")
            task["_cancel"] = True
            if task["status"]["state"] == "submitted":
                self.update(task, "canceled")
            self.condition.notify_all()
        # A working task becomes canceled only after Codex confirms interruption.
        return task

    def run(self):
        while not self.stopping.is_set():
            try:
                task = self.work.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                with self.condition:
                    if task["_cancel"]:
                        continue
                    self.update(task, "working")
                self.execute(task)
            except Exception as error:
                self.update(task, "failed", str(error))
            finally:
                self.work.task_done()

    def execute(self, task):
        context = task["contextId"]
        thread_id = self.contexts[context]
        if thread_id is None:
            result = self.codex.call("thread/start", {"cwd": self.cwd, "model": self.model,
                "approvalPolicy": "never", "sandbox": "workspace-write"})
            thread_id = result["thread"]["id"]
            self.contexts[context] = thread_id
        turn = self.codex.call("turn/start", {"threadId": thread_id,
            "input": [{"type": "text", "text": task["_text"]}]})["turn"]
        turn_id = turn["id"]
        deadline = time.monotonic() + self.timeout
        interrupt_deadline = None
        timed_out = False
        messages, diff = {}, ""
        while True:
            if self.stopping.is_set():
                raise RuntimeError("Adapter stopped")
            if self.codex.proc.poll() is not None:
                raise RuntimeError("Codex stopped; restart the adapter")
            now = time.monotonic()
            if interrupt_deadline is None and (task["_cancel"] or now >= deadline):
                timed_out = not task["_cancel"]
                self.codex.call("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
                interrupt_deadline = time.monotonic() + 10
            if interrupt_deadline is not None and now >= interrupt_deadline:
                self.codex.close()
                raise RuntimeError("Codex did not confirm interruption; stopped to prevent overlapping work")
            try:
                event = self.codex.events.get(timeout=0.25)
            except queue.Empty:
                continue
            params = event.get("params", {})
            if params.get("threadId") != thread_id or params.get("turnId", params.get("turn", {}).get("id")) != turn_id:
                continue
            method = event["method"]
            if "id" in event:
                if method not in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval", "item/permissions/requestApproval"):
                    self.codex.call("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
                    interrupt_deadline = time.monotonic() + 10
                    timed_out = True
                self.update(task, text="Interactive request declined: " + method)
            elif method == "item/agentMessage/delta":
                item_id = params["itemId"]
                messages[item_id] = (messages.get(item_id, "") + params.get("delta", ""))[:MAX_OUTPUT]
                self.update(task, text=messages[item_id])
            elif method == "item/completed":
                item = params.get("item", {})
                if item.get("type") == "agentMessage":
                    messages[item["id"]] = item.get("text", "")[:MAX_OUTPUT]
                    self.update(task, text=messages[item["id"]])
            elif method == "turn/diff/updated":
                diff = params.get("diff", "")[:MAX_OUTPUT]
            elif method == "turn/completed":
                result = params["turn"]
                text = "\n\n".join(messages.values())[:MAX_OUTPUT]
                with self.condition:
                    for name, value in (("response", text), ("diff", diff)):
                        if value:
                            task["artifacts"].append({"artifactId": str(uuid4()), "name": name,
                                "parts": [{"kind": "text", "text": value}]})
                    if timed_out:
                        self.update(task, "failed", "Task timed out or required unsupported interaction")
                    elif result["status"] == "interrupted":
                        self.update(task, "canceled")
                    elif result["status"] == "completed":
                        self.update(task, "completed", text or "Task completed")
                    else:
                        self.update(task, "failed", result.get("error", {}).get("message", "Codex turn failed"))
                return

    def close(self):
        self.stopping.set()
        self.codex.close()
        self.worker.join(timeout=3)


def agent_card(public_url):
    return {"protocolVersion": "0.3.0", "name": "Local Codex coder", "version": "1.0.0",
        "description": "Codes in one fixed workspace using a local Codex CLI.",
        "url": public_url.rstrip("/") + "/agents/coder", "preferredTransport": "JSONRPC",
        "capabilities": {"streaming": True, "pushNotifications": False},
        "defaultInputModes": ["text/plain"], "defaultOutputModes": ["text/plain"],
        "securitySchemes": {"vaultJwt": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}},
        "security": [{"vaultJwt": []}], "skills": [{"id": "coder", "name": "Coding",
            "description": "Edit code, run checks, and continue a coding conversation.", "tags": ["code"]}]}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def reply(self, status, body=None):
        data = b"" if body is None else json.dumps(body, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        tokens = self.headers.get_all("x-codex-agent-token", [])
        if len(tokens) != 1 or not hmac.compare_digest(tokens[0], self.server.token):
            self.close_connection = True
            self.reply(401)
            return False
        return True

    def do_GET(self):
        if self.path == "/health":
            ready = self.server.agent.codex.proc.poll() is None
            self.reply(200 if ready else 503, {"status": "ok" if ready else "stopped"})
        elif self.authorized():
            if self.path == "/agents/coder/.well-known/agent-card.json":
                self.reply(200, agent_card(self.server.public_url))
            else:
                self.reply(404)

    def do_POST(self):
        if not self.authorized():
            return
        if self.path != "/agents/coder":
            self.reply(404)
            return
        request_id = None
        try:
            self.connection.settimeout(30)
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise RpcError(-32600, "Invalid Content-Length") from None
            if self.headers.get("Transfer-Encoding") or not 0 < length <= 131072:
                self.close_connection = True
                self.reply(413)
                return
            try:
                request = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeDecodeError):
                raise RpcError(-32700, "Parse error") from None
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str) or "id" not in request or type(request["id"]) not in (str, int):
                raise RpcError(-32600, "Invalid Request")
            request_id = request["id"]
            params = request.get("params", {})
            if not isinstance(params, dict):
                raise RpcError(-32602, "params must be an object")
            method = request["method"]
            agent = self.server.agent
            if method in ("message/send", "message/stream"):
                config = params.get("configuration", {})
                if not isinstance(config, dict) or type(config.get("blocking", True)) is not bool:
                    raise RpcError(-32602, "configuration.blocking must be boolean")
                task = agent.submit(params)
                if method == "message/stream":
                    self.stream(request_id, task)
                    return
                if config.get("blocking", True):
                    with agent.condition:
                        agent.condition.wait_for(lambda: task["status"]["state"] in TERMINAL, timeout=25)
            elif method == "tasks/get":
                task = agent.get(params)
            elif method == "tasks/cancel":
                task = agent.cancel(params)
            elif method == "tasks/resubscribe":
                self.stream(request_id, agent.get(params))
                return
            else:
                raise RpcError(-32601, "Method not found")
            with agent.condition:
                self.reply(200, {"jsonrpc": "2.0", "id": request_id, "result": agent.snapshot(task)})
        except RpcError as error:
            self.reply(200, {"jsonrpc": "2.0", "id": request_id,
                "error": {"code": error.code, "message": str(error)}})
        except UnicodeError:
            self.reply(200, {"jsonrpc": "2.0", "id": request_id,
                "error": {"code": -32602, "message": "Invalid UTF-8 input"}})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass  # Client disconnects do not cancel a task; use tasks/cancel explicitly.

    def stream(self, request_id, task):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        agent = self.server.agent
        def emit(result):
            self.wfile.write(("data: " + json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}) + "\n\n").encode())
            self.wfile.flush()
        with agent.condition:
            version = task["_version"]
            snapshot = agent.snapshot(task)
        emit(snapshot)
        while snapshot["status"]["state"] not in TERMINAL:
            with agent.condition:
                changed = agent.condition.wait_for(lambda: task["_version"] != version or agent.stopping.is_set(), timeout=10)
                version = task["_version"]
                snapshot = agent.snapshot(task)
            if agent.stopping.is_set():
                return
            if not changed:
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
                continue
            final = snapshot["status"]["state"] in TERMINAL
            if final:
                for artifact in snapshot["artifacts"]:
                    emit({"kind": "artifact-update", "taskId": task["id"], "contextId": task["contextId"],
                          "artifact": artifact, "append": False, "lastChunk": True})
            emit({"kind": "status-update", "taskId": task["id"], "contextId": task["contextId"],
                  "status": snapshot["status"], "final": final})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default=os.environ.get("CODEX_WORKSPACE", os.getcwd()))
    parser.add_argument("--host", default=os.environ.get("CODEX_AGENT_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=8082)
    args = parser.parse_args()
    token = os.environ.get("CODEX_AGENT_TOKEN", "")
    if len(token) < 32 or not token.isascii():
        parser.error("CODEX_AGENT_TOKEN must be a private ASCII token of at least 32 characters (make setup)")
    cwd = Path(args.workspace).resolve(strict=True)
    if not cwd.is_dir():
        parser.error("workspace must be a directory")
    model = os.environ.get("CODEX_MODEL", "gpt-6-luna")
    base_url = os.environ.get("CODEX_MODEL_BASE_URL", "http://localhost:4000/v1")
    os.environ.setdefault("CODEX_MODEL_API_KEY", "test")
    # JSON string syntax is also valid TOML string syntax here.
    provider = '{name="Agentgateway",base_url=' + json.dumps(base_url) + ',wire_api="responses",requires_openai_auth=false,env_http_headers={"x-api-key"="CODEX_MODEL_API_KEY"}}'
    command = ["codex", "app-server", "-c", 'model_provider="agw"',
               "-c", "model_providers.agw=" + provider]
    backend = Codex(command, str(cwd))
    agent = Agent(backend, cwd, model)
    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
        server.daemon_threads = True
        server.agent, server.token = agent, token
        server.public_url = os.environ.get("CODEX_AGENT_PUBLIC_URL", "http://localhost:4000")
        print(f"Codex coder ready on {args.host}:{args.port}; workspace={cwd}", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        agent.close()


if __name__ == "__main__":
    main()
