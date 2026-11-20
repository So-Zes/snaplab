"""A synthetic SSH peer: real pipes, no sockets, Linux, model calls or credentials."""
import base64
import hashlib
import json
import sys
import time


def emit(**event):
    print(json.dumps(event), flush=True)


request = json.loads(sys.stdin.buffer.readline())
# A new job folder: the worker has none of the files yet, so it asks for every one in the manifest.
emit(kind="need", paths=sorted(request["manifest"]), local=0)
files = {}
while True:
    item = json.loads(sys.stdin.buffer.readline())
    if item.get("kind") == "start":
        break
    raw = base64.b64decode(item["data"])
    assert hashlib.sha256(raw).hexdigest() == request["manifest"][item["path"]]
    files[item["path"]] = raw
assert set(files) == set(request["manifest"])
sent = dict(files)
assert "private-chat-canary" not in json.dumps(request)
assert "private-memory-canary" not in str(files)
assert "synthetic-real-provider-key" not in str(files)
assert ".git" not in files and ".snaplab/STATE.md" not in files
assert request.get("web_token", "synthetic") not in request["config"]
assert "default_permissions" not in request["config"]
assert 'wire_api = "responses"' in request["config"]
assert request["token"] not in request["config"]
prompt = request["prompt"]
if "fixture:hang" in prompt:
    time.sleep(60)
    sys.exit(1)
if "fixture:disconnect" in prompt:
    emit(kind="file", path="partial.txt", data=base64.b64encode(b"partial").decode())
    sys.exit(255)
emit(kind="codex", event={"type": "item.completed", "item": {"type": "agent_message", "text": "Screenshot created."}})
emit(kind="codex", event={"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}})
if "fixture:traversal" in prompt:
    files["../outside.txt"] = b"refuse this"
elif "fixture:secret" in prompt:
    files["leak.txt"] = request["token"].encode()
else:
    files.pop("delete.txt", None)
    files["screenshot.png"] = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aY1sAAAAASUVORK5CYII=")
    files["index.html"] = b"<h1>Synthetic result</h1>"
    files[".snaplab/attachments.json"] = json.dumps([{"path": "screenshot.png", "caption": "Screen"},
                                                  {"path": "index.html", "caption": "Page"}]).encode()
    files[".snaplab/PLAN.md"] = b"- [x] Make a screenshot\n"
if request["test_command"]:
    emit(kind="tests", exit_code=0, output="2 passed")
for name, raw in files.items():
    if sent.get(name) != raw:
        emit(kind="file", path=name, data=base64.b64encode(raw).decode())
emit(kind="deleted", paths=sorted(n for n in sent if n not in files))
emit(kind="complete", exit_code=1 if "fixture:codex-failed" in prompt else 0)
