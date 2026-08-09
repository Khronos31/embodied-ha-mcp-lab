"""Small controllable stdio MCP server used only by runner tests."""

import json
import os
import sys
import time


def send(value):
    print(json.dumps(value, separators=(",", ":")), flush=True)


for line in sys.stdin:
    try:
        request = json.loads(line)
    except json.JSONDecodeError:
        continue
    if not isinstance(request, dict):
        continue
    method = request.get("method")
    request_id = request.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": request_id, "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "dummy", "version": "1"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": [{"name": "echo", "description": "Echo arguments", "inputSchema": {"type": "object", "properties": {"value": {"type": "string"}}}}]}})
    elif method == "tools/call":
        mode = os.environ.get("DUMMY_MODE", "echo")
        if mode == "hang":
            time.sleep(60)
        elif mode == "flood":
            sys.stderr.write("x" * int(os.environ.get("DUMMY_FLOOD_BYTES", "4096")))
            sys.stderr.flush()
            time.sleep(60)
        else:
            send({"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": json.dumps(request.get("params", {}).get("arguments"), separators=(",", ":"))}]}})
