"""A tiny MCP server over stdio for tests: initialize, tools/list, tools/call (echo, env_set) and a ping to the client."""
import json
import os
import sys

TOOLS = [
    {"name": "echo", "description": "Echo the text back.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "env_set", "description": "Say whether FIXTURE_SECRET is set (never its value).",
     "inputSchema": {"type": "object", "properties": {}}},
]


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


print("fixture server starting", file=sys.stderr, flush=True)
for line in sys.stdin:
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    if "id" not in msg or "method" not in msg:
        continue                      # notifications and the client's replies to our ping
    method, rid = msg["method"], msg["id"]
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                                                      "serverInfo": {"name": "fixture", "version": "1"},
                                                      "instructions": "A test server with an echo tool."}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": "ping-1", "method": "ping"})        # a request from the server to the client
        send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        name = msg["params"]["name"]
        args = msg["params"].get("arguments") or {}
        if name == "echo":
            text = "echo: " + str(args.get("text"))
        else:
            text = "set" if os.environ.get("FIXTURE_SECRET") else "not set"
        send({"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": text}]}})
    else:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "unknown method"}})
