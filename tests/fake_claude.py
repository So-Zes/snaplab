"""Stands in for `claude -p --output-format stream-json`: reads the prompt, calls SnapLab's bridge, streams a reply.

Behaviour comes from the prompt text: "TOOL:<name>:<json args>" calls that tool through the MCP config it was given;
"BADTOKEN" first tries the bridge with a wrong token; "FAIL" ends with an error result; "SLOW" waits for a stop.
"""
import json
import sys
import time
import urllib.error
import urllib.request

args = sys.argv[1:]
user = json.loads(sys.stdin.readline())
prompt = "".join(b.get("text", "") for b in user["message"]["content"] if b.get("type") == "text")
system = open(args[args.index("--system-prompt-file") + 1], encoding="utf-8").read()


def out(ev):
    print(json.dumps(ev, ensure_ascii=False), flush=True)


def rpc(url, headers, method, params, i):
    req = urllib.request.Request(url, data=json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params}).encode(),
                                 headers={"Content-Type": "application/json", **headers}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def delta(text, kind="text"):
    d = {"type": "text_delta", "text": text} if kind == "text" else {"type": "thinking_delta", "thinking": text}
    out({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": d}})


out({"type": "system", "subtype": "init", "tools": [], "mcp_servers": []})
out({"type": "stream_event", "event": {"type": "message_start"}})
delta("thinking...", "thinking")
report = {"args": args, "system_has_note": "mcp__snaplab__" in system, "prompt": prompt}
if "--mcp-config" in args:
    cfg = json.load(open(args[args.index("--mcp-config") + 1], encoding="utf-8"))["mcpServers"]["snaplab"]
    url, headers = cfg["url"], cfg["headers"]
    if "BADTOKEN" in prompt:
        try:
            rpc(url, {"Authorization": "Bearer nope"}, "tools/list", {}, 0)
            report["bad_token"] = "accepted"
        except urllib.error.HTTPError as e:
            report["bad_token"] = e.code
    rpc(url, headers, "initialize", {"protocolVersion": "2025-06-18"}, 1)
    report["tools"] = [t["name"] for t in rpc(url, headers, "tools/list", {}, 2)["result"]["tools"]]
    i = 3
    for part in prompt.split("TOOL:")[1:]:
        name, raw = part.split(":", 1)
        raw = raw.split("\n")[0].strip()
        delta("Let me check.")
        out({"type": "stream_event", "event": {"type": "message_start"}})
        res = rpc(url, headers, "tools/call", {"name": name, "arguments": json.loads(raw)}, i)["result"]
        report.setdefault("results", []).append([name, res["isError"], res["content"][0]["text"][:200]])
        i += 1
if "SLOW" in prompt:
    time.sleep(30)
if "FAIL" in prompt:
    out({"type": "result", "subtype": "success", "is_error": True, "result": "Not logged in · Please run /login"})
    sys.exit(1)
answer = "REPORT " + json.dumps(report, ensure_ascii=False)
delta(answer)
out({"type": "result", "subtype": "success", "is_error": False, "result": answer,
     "usage": {"input_tokens": 3, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 7, "output_tokens": 11}})
