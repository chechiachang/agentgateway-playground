"""Minimal app-server peer for adapter integration tests; no model or credentials."""
import json
import sys
import threading
import time

write_lock = threading.Lock()
active = {}
turns = {}
threads = 0

def emit(message):
    with write_lock:
        print(json.dumps(message), flush=True)

def finish(thread_id, turn_id, text, delay):
    time.sleep(delay)
    if active.get(thread_id) != turn_id:
        return
    params = {"threadId": thread_id, "turnId": turn_id}
    if text == "approval":
        emit({"id": "approval-1", "method": "item/commandExecution/requestApproval", "params": params})
    emit({"method": "item/agentMessage/delta", "params": {**params, "itemId": turn_id, "delta": "done " + text}})
    emit({"method": "turn/diff/updated", "params": {**params, "diff": "+changed"}})
    emit({"method": "turn/completed", "params": {"threadId": thread_id, "turn": {"id": turn_id, "status": "failed" if text == "fail" else "completed", "error": {"message": "test failure"}}}})
    active.pop(thread_id, None)

for line in sys.stdin:
    request = json.loads(line)
    method, params = request.get("method"), request.get("params", {})
    if method is None:
        if request.get("id") == "approval-1" and request.get("result") != {"decision": "decline"}:
            sys.exit(2)
        continue
    if method == "initialized":
        continue
    if method == "initialize":
        result = {}
    elif method == "thread/start":
        assert params["sandbox"] == "workspace-write" and params["approvalPolicy"] == "never"
        threads += 1
        result = {"thread": {"id": "thread-" + str(threads)}}
    elif method == "turn/start":
        thread_id = params["threadId"]
        turns[thread_id] = turns.get(thread_id, 0) + 1
        turn_id = thread_id + "-turn-" + str(turns[thread_id])
        active[thread_id] = turn_id
        text = params["input"][0]["text"]
        if text == "crash":
            sys.exit(1)
        result = {"turn": {"id": turn_id, "status": "inProgress"}}
        threading.Thread(target=finish, args=(thread_id, turn_id, text, 5 if text == "slow" else .15), daemon=True).start()
    elif method == "turn/interrupt":
        result = {}
        active.pop(params["threadId"], None)
        emit({"method": "turn/completed", "params": {"threadId": params["threadId"], "turn": {"id": params["turnId"], "status": "interrupted"}}})
    else:
        result = {}
    emit({"id": request["id"], "result": result})
