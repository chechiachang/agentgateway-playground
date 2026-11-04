#!/usr/bin/env python3
"""Exercise request guardrails without sending a request to the model provider."""
import json
import os
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

base_url = os.environ.get("AGW_URL", "http://localhost:4000").rstrip("/")
headers = {"Content-Type": "application/json"}
if os.environ.get("JWT"):
    headers["Authorization"] = "Bearer " + os.environ["JWT"]
else:
    headers["x-api-key"] = "test"

for model in ("gpt-6-luna-guarded", "gpt-6.1-sol-guarded"):
    for label, message in (
        ("demo secret", {"role": "user", "content": "AGW_DEMO_SECRET_123ABC"}),
        ("SSN", {"role": "user", "content": "123-45-6789"}),
        ("system prompt", {"role": "system", "content": "AGW_DEMO_SECRET_123ABC"}),
        ("tool output", {"role": "tool", "tool_call_id": "demo", "content": "AGW_DEMO_SECRET_123ABC"}),
    ):
        payload = {"model": model, "messages": [message], "stream": False}
        request = Request(base_url + "/v1/chat/completions", json.dumps(payload).encode(), headers)
        try:
            with urlopen(request, timeout=20) as response:
                status, body = response.status, response.read()
        except HTTPError as error:
            status, body = error.code, error.read()
        if status != 403 or body != b"Guardrail rejected sensitive input.":
            sys.exit(f"FAIL {model} / {label}: HTTP {status}; expected guardrail rejection. "
                     "Merge chat guardrails into config/agentgateway.yaml and restart the gateway.")
        print(f"PASS {model} / {label}: rejected before inference")
    # Codex uses Responses, which also resends conversation history.
    payload = {"model": model, "input": [
        {"role": "user", "content": "AGW_DEMO_SECRET_123ABC"},
        {"role": "assistant", "content": "This is a demo token."},
        {"role": "user", "content": "Continue."},
    ], "stream": True}
    request = Request(base_url + "/v1/responses", json.dumps(payload).encode(), headers)
    try:
        with urlopen(request, timeout=20) as response:
            status, body = response.status, response.read()
    except HTTPError as error:
        status, body = error.code, error.read()
    if status != 403 or body != b"Guardrail rejected sensitive input.":
        sys.exit(f"FAIL {model} / Responses history: HTTP {status}; expected guardrail rejection")
    print(f"PASS {model} / Responses history: rejected before inference")
