"""Stands in for `remote_worker.py` in live mode and the browser behind it: answers DevTools commands like Chrome.

Every command and control line is appended to the file named by the first argument (one JSON object per line)."""
import json
import os
import sys

LOG = sys.argv[1]
sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")


def log(obj):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj) + "\n")


def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def cdp(m):
    out({"kind": "cdp", "m": m})


req = json.loads(sys.stdin.readline())
log({"request": {k: v for k, v in req.items() if k != "helpers"}, "helpers": sorted(req.get("helpers") or {})})
out({"kind": "connected", "host": bool(req.get("host"))})
for line in sys.stdin:
    msg = json.loads(line)
    log(msg)
    if msg.get("snaplab"):
        continue
    method, mid, session = msg.get("method"), msg.get("id"), msg.get("sessionId")
    result = {}
    events = []
    if method == "Target.getTargets":
        result = {"targetInfos": [{"targetId": "T1", "type": "page", "url": "https://example.com/", "title": "Example"},
                                  {"targetId": "D1", "type": "page", "url": "devtools://devtools/x", "title": "DevTools"}]}
    elif method == "Target.attachToTarget":
        result = {"sessionId": "S-" + msg["params"]["targetId"]}
    elif method == "Page.getLayoutMetrics":
        result = {"cssVisualViewport": {"clientWidth": 1000, "clientHeight": 500, "pageX": 0, "pageY": 0}}
    elif method == "Page.captureScreenshot":
        result = {"data": "AAAA"}
    elif method == "Page.startScreencast":
        events.append({"method": "Page.screencastFrame", "sessionId": session,
                       "params": {"data": "BBBB", "sessionId": 7, "metadata": {"deviceWidth": 1000, "deviceHeight": 500}}})
    elif method == "Page.navigate":
        result = {"frameId": "F1"}
    elif method == "Page.getNavigationHistory":
        result = {"currentIndex": 1, "entries": [{"id": 11, "url": "https://a.example/"}, {"id": 12, "url": "https://example.com/"}]}
    elif method == "Target.createTarget":
        result = {"targetId": "T2"}
        events.append({"method": "Target.targetCreated", "params": {"targetInfo": {"targetId": "T2", "type": "page",
                                                                                   "url": "about:blank", "title": ""}}})
    elif method == "Runtime.evaluate":
        result = {"result": {"value": "visible"}}
    if mid:
        cdp({"id": mid, "result": result})
    for ev in events:
        cdp(ev)
    if method == "Browser.close":
        break
out({"kind": "disconnected"})
