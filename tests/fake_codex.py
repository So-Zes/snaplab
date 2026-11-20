"""Stands in for `codex exec --json`: reads the prompt on stdin, calls SnapLab's bridge, prints JSONL events.

Behaviour comes from the prompt text: "TOOL:<name>:<json args>" calls that tool through the MCP server given with
-c mcp_servers.snaplab.*; "LIMIT" ends with a usage-limit failure; "SLOW" waits for a stop. The last line printed
before turn.completed is a REPORT with the arguments, environment names and tool results.
"""
import json
import os
import sys
import time
import urllib.request

args = sys.argv[1:]
prompt = sys.stdin.read()
newest = prompt.rsplit("The newest message follows. Reply to it.", 1)[-1]     # earlier turns are history
values = [args[i + 1] for i, a in enumerate(args) if a == "-c"]


def value(key):
    for v in values:
        if v.startswith(key + "="):
            raw = v.split("=", 1)[1]
            try:
                return json.loads(raw)
            except ValueError:
                return raw
    return None


def out(ev):
    print(json.dumps(ev, ensure_ascii=False), flush=True)


def rpc(url, token, method, params, i):
    req = urllib.request.Request(url, data=json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params}).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


out({"type": "thread.started", "thread_id": "fake-thread"})
out({"type": "turn.started"})
instructions = open(value("model_instructions_file"), encoding="utf-8").read()
report = {"args": args, "env": sorted(os.environ), "prompt": prompt, "instructions_has_note": "SnapLab MCP server" in instructions,
          "persona": instructions.split("\n", 1)[0][:200]}
url = value("mcp_servers.snaplab.url")
if url:
    token = os.environ.get(value("mcp_servers.snaplab.bearer_token_env_var") or "", "")
    rpc(url, token, "initialize", {"protocolVersion": "2025-06-18"}, 1)
    report["tools"] = [t["name"] for t in rpc(url, token, "tools/list", {}, 2)["result"]["tools"]]
    i = 3
    for part in newest.split("TOOL:")[1:]:
        name, raw = part.split(":", 1)
        raw = raw.split("\n")[0].strip()
        out({"type": "item.completed", "item": {"id": f"m{i}", "type": "agent_message", "text": "Let me check."}})
        out({"type": "item.started", "item": {"id": f"t{i}", "type": "mcp_tool_call", "server": "snaplab", "tool": name}})
        res = rpc(url, token, "tools/call", {"name": name, "arguments": json.loads(raw)}, i)["result"]
        out({"type": "item.completed", "item": {"id": f"t{i}", "type": "mcp_tool_call", "server": "snaplab", "tool": name,
                                                 "status": "completed"}})
        report.setdefault("results", []).append([name, res["isError"], res["content"][0]["text"][:300]])
        i += 1
if "SLOW" in newest:
    time.sleep(30)
if "LIMIT" in newest:
    out({"type": "turn.failed", "error": {"message": "You've hit your usage limit. Try again later."}})
    sys.exit(1)
out({"type": "item.completed", "item": {"id": "r1", "type": "reasoning", "text": "thinking..."}})
out({"type": "item.completed", "item": {"id": "m-final", "type": "agent_message",
                                         "text": "Done. REPORT " + json.dumps(report, ensure_ascii=False)}})
out({"type": "turn.completed", "usage": {"input_tokens": 120, "cached_input_tokens": 20, "output_tokens": 15}})
