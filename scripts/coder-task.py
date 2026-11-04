#!/usr/bin/env python3
"""Submit/poll/cancel coder tasks through AGW using an agents:coder JWT."""
import argparse
import json
import os
import sys
from urllib.request import Request, urlopen
from uuid import uuid4

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("action", choices=("send", "stream", "get", "cancel"))
parser.add_argument("input", help="instruction for send/stream; task ID for get/cancel")
parser.add_argument("--context", help="returned contextId for a follow-up instruction")
args = parser.parse_args()
token = os.environ.get("JWT")
if not token:
    parser.error("export JWT from a Vault identity with agents:coder access")
if args.action in ("send", "stream"):
    method = "message/send" if args.action == "send" else "message/stream"
    message = {"role": "user", "messageId": str(uuid4()), "parts": [{"kind": "text", "text": args.input}]}
    if args.context:
        message["contextId"] = args.context
    params = {"message": message, "configuration": {"blocking": False}}
else:
    method, params = "tasks/" + args.action, {"id": args.input}
body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
url = os.environ.get("AGW_URL", "http://localhost:4000").rstrip("/") + "/agents/coder"
request = Request(url, data=json.dumps(body).encode(), headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
try:
    with urlopen(request, timeout=60) as response:
        if args.action == "stream":
            for line in response:
                sys.stdout.write(line.decode())
                sys.stdout.flush()
        else:
            result = json.load(response)
            print(json.dumps(result, indent=2))
            if "error" in result:
                sys.exit(1)
except OSError as error:
    parser.exit(1, f"Coder request failed: {error}\n")
