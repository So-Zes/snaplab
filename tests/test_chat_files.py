"""Files and photos the user sends to a chat: upload checks, the message, what the bot's model receives,
read_attachment, and handing files to SnapCode. Temporary data and a scripted model only."""
import base64
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from helpers import make_app, onboard, wait_until
from snaplab import server, uploads
from snaplab.agent import ToolResult, run_loop
from snaplab.tools import Toolbox


def photo(width=3000, height=2000, mode="RGB"):
    from PIL import Image
    out = io.BytesIO()
    Image.new(mode, (width, height), (200, 30, 40) if mode == "RGB" else (200, 30, 40, 128)).save(out, "PNG")
    return out.getvalue()


def pdf(text):
    stream = f"BT /F1 18 Tf 20 100 Td ({text}) Tj ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream", "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode()
    xref = len(out)
    out += (f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n" + "".join(f"{o:010d} 00000 n \n" for o in offsets)
            + f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF").encode()
    return out


def docx(*paragraphs):
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("word/document.xml", f'<w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>')
    return out.getvalue()


class ChatFilesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.seen = []

        def responder(messages, tools, effort):
            cls.seen.append(messages)
            return {"content": "Got it."}
        cls.app, cls.llm, _ = make_app(responder, tmp=cls.tmp.name)
        cls.addClassCleanup(cls.app.archive.close)
        cls.app.public_url = "http://test.local"
        token, _ = cls.app.auth.new_pairing("test")
        cls.cookie = None
        cls.cookie = cls.req("POST", "/api/pair", json.dumps({"token": token}).encode(), cookie=False,
                             headers={"Content-Type": "application/json", "X-SnapLab": "1"})[2]["Set-Cookie"].split(";")[0]
        r = onboard(cls.app)
        cls.room, cls.bot = r["room"], r["bot"]

    @classmethod
    def req(cls, method, path, body=b"", *, cookie=True, headers=None):
        h = {"Host": "test.local", "Connection": "close", "Tailscale-User-Login": "owner@example.com"}
        if cookie and cls.cookie:
            h["Cookie"] = cls.cookie
        if body or method == "POST":
            h["Content-Length"] = str(len(body))
        h.update(headers or {})
        request = (f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items()) + "\r\n").encode() + body

        class Connection:
            output = bytearray()

            def makefile(self, *args):
                return io.BytesIO(request)

            def sendall(self, raw):
                self.output.extend(raw)
        connection = Connection()
        handler = type("TestHandler", (server.Handler,), {"app": cls.app})
        handler(connection, ("127.0.0.1", 12345), None)
        head, raw = bytes(connection.output).split(b"\r\n\r\n", 1)
        lines = head.decode().split("\r\n")
        return int(lines[0].split()[1]), raw, dict(line.split(": ", 1) for line in lines[1:])

    def upload(self, name, raw, mime="application/octet-stream", room=None, **extra):
        headers = {"Content-Type": mime, "X-SnapLab": "1", "X-SnapLab-Name": __import__("urllib.parse").parse.quote(name)}
        headers.update(extra)
        return self.req("POST", f"/api/rooms/{room or self.room['id']}/files", raw, headers=headers)

    def sent(self, name, raw, mime="application/octet-stream"):
        status, body, _ = self.upload(name, raw, mime)
        self.assertEqual(status, 200, body)
        return json.loads(body)["file"]

    def send(self, text, files):
        return self.req("POST", f"/api/rooms/{self.room['id']}/send", json.dumps({"text": text, "files": files}).encode(),
                        headers={"Content-Type": "application/json", "X-SnapLab": "1"})

    def test_upload_checks_like_every_other_post(self):
        self.assertEqual(self.upload("a.txt", b"hi")[0], 200)
        self.assertEqual(self.req("POST", f"/api/rooms/{self.room['id']}/files", b"hi", cookie=False,
                                  headers={"X-SnapLab": "1"})[0], 401)
        self.assertEqual(self.req("POST", f"/api/rooms/{self.room['id']}/files", b"hi", headers={})[0], 403)
        self.assertEqual(self.upload("a.txt", b"hi", Origin="https://evil.example")[0], 403)
        self.assertEqual(self.upload("a.txt", b"hi", **{"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.upload("a.txt", b"hi", room="r-00000000")[0], 404)
        self.assertEqual(self.upload("a.txt", b"")[0], 400)

    def test_kinds_names_and_serving(self):
        image = self.sent("IMG_0001.PNG", photo(), "image/png")
        self.assertEqual((image["kind"], image["width"], image["height"]), ("image", 3000, 2000))
        text = self.sent("メモ.txt", "こんにちは\n買うもの: 牛乳".encode(), "text/plain")
        self.assertEqual(text["kind"], "text")
        sjis = self.sent("old.csv", "名前,数\nりんご,3".encode("cp932"), "text/csv")
        self.assertEqual(sjis["kind"], "text")
        paper = self.sent("paper.pdf", pdf("Hello from the PDF"), "application/pdf")
        self.assertEqual(paper["kind"], "pdf")
        blob = self.sent("../../evil\x00name.bin", bytes(range(256)) * 10)
        self.assertEqual((blob["kind"], blob["name"]), ("file", "evilname.bin"))
        svg = self.sent("logo.svg", b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>", "image/svg+xml")
        self.assertEqual(svg["kind"], "text")            # read as text, never shown as a picture
        status, raw, headers = self.req("GET", f"/api/attachments/{image['id']}/content")
        self.assertEqual((status, headers["Content-Type"], raw[:8]), (200, "image/png", b"\x89PNG\r\n\x1a\n"))
        self.assertIn("sandbox", headers["Content-Security-Policy"])
        status, raw, headers = self.req("GET", f"/api/attachments/{blob['id']}/download")
        self.assertEqual((status, headers["Content-Type"], raw), (200, "application/octet-stream", bytes(range(256)) * 10))
        self.assertTrue(headers["Content-Disposition"].startswith("attachment;"))
        self.assertEqual(self.req("GET", f"/api/attachments/{blob['id']}/preview")[0], 404)
        self.assertEqual(self.req("GET", f"/api/attachments/{svg['id']}/content")[2]["Content-Type"], "application/octet-stream")
        self.assertEqual(self.req("GET", f"/api/attachments/{image['id']}/content", cookie=False)[0], 401)
        self.assertEqual(uploads.text_of(self.app, uploads.get(self.app, sjis["id"]))[0], "名前,数\nりんご,3")
        self.assertIn("Hello from the PDF", uploads.text_of(self.app, uploads.get(self.app, paper["id"]))[0])
        word = self.sent("plan.docx", docx("First line", "第二の段落"))
        self.assertEqual(uploads.text_of(self.app, uploads.get(self.app, word["id"]))[0], "First line\n第二の段落")

    def test_send_discard_and_room_list(self):
        a = self.sent("a.txt", b"alpha")
        b = self.sent("b.txt", b"beta")
        self.assertEqual(self.send("", [])[0], 400)
        self.assertTrue(json.loads(self.req("POST", f"/api/files/{b['id']}/discard", b"{}", headers={
            "Content-Type": "application/json", "X-SnapLab": "1"})[1])["removed"])
        self.assertIsNone(uploads.get(self.app, b["id"]))
        self.assertEqual(self.send("", [b["id"]])[0], 400)
        status, raw, _ = self.send("", [a["id"]])
        self.assertEqual(status, 200, raw)
        msg = json.loads(raw)["message"]
        self.assertEqual((msg["text"], msg["attachments"][0]["id"], msg["preview"]), ("", a["id"], "a.txt"))
        self.assertEqual(self.app.store.room(self.room["id"])["last_text"], "a.txt")
        self.assertFalse(uploads.discard(self.app, a["id"]))       # a sent file stays with the history
        listed = json.loads(self.req("GET", f"/api/rooms/{self.room['id']}/attachments")[1])["attachments"]
        self.assertIn(a["id"], [x["id"] for x in listed])
        other = self.app.create_group("Other", [self.bot["id"]])
        foreign = json.loads(self.upload("c.txt", b"gamma", room=other["id"])[1])["file"]
        self.assertEqual(self.send("look", [foreign["id"]])[0], 400)    # another room's upload
        wait_until(lambda: not self.app.chat.busy(self.room["id"]), 10)

    def test_the_model_sees_the_photo_and_the_text(self):
        image = self.sent("shelf.png", photo(4000, 3000, "RGBA"), "image/png")
        note = self.sent("notes.md", ("# Plan\n" + "line\n" * 50).encode(), "text/markdown")
        before = len(self.seen)
        self.assertEqual(self.send("この写真を見て", [image["id"], note["id"]])[0], 200)
        wait_until(lambda: len(self.seen) > before, 10)
        wait_until(lambda: not self.app.chat.busy(self.room["id"]), 10)
        last = self.seen[before][-1]["content"]
        self.assertIsInstance(last, list)
        texts = "".join(p["text"] for p in last if p["type"] == "text")
        self.assertIn(f"[File from the user: shelf.png — image 4000×3000", texts)
        self.assertIn("この写真を見て", texts)
        self.assertIn("Contents of notes.md:\n<<<\n# Plan\nline", texts)
        url = next(p["image_url"]["url"] for p in last if p["type"] == "image_url")
        self.assertTrue(url.startswith("data:image/jpeg;base64,"))
        from PIL import Image
        view = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
        self.assertEqual(view.size, (1600, 1200))
        self.assertEqual(view.getpixel((5, 5))[0] > 200, True)        # transparent parts on white, not black

    def test_a_model_without_vision_is_told(self):
        model = self.app.models.name_for(None, "chat")
        self.app.cfg["models"][model]["capabilities"] = {"vision": False}
        try:
            image = self.sent("receipt.png", photo(64, 64), "image/png")
            msgs, _ = self.app.chat.context.build(self.app.store.room(self.room["id"]), self.bot, "en")
            msgs[-1]["content"] = uploads.line(uploads.get(self.app, image["id"])) + "\n" + msgs[-1]["content"]
            uploads.expand(self.app, msgs, vision=uploads.can_see(self.app, self.bot))
            self.assertIsInstance(msgs[-1]["content"], str)
            self.assertIn("Your model cannot see images", msgs[-1]["content"])
            self.assertIn(f'files=["{image["id"]}"]', msgs[-1]["content"])
        finally:
            self.app.cfg["models"][model].pop("capabilities")

    def test_read_attachment_text_image_region(self):
        long = self.sent("long.txt", ("あ" * 5000 + "END").encode())
        image = self.sent("map.png", photo(2400, 1200), "image/png")
        room = self.app.store.room(self.room["id"])
        box = Toolbox(self.app, self.bot, room=room, lang="en")
        res = box("read_attachment", {"file": long["id"], "from_char": 4990, "max_chars": 1000}, "c1")
        self.assertIn("ああEND", res.content)
        self.assertIn("[end of file]", res.content)
        res = box("read_attachment", {"file": image["id"], "region": [100, 100, 400, 300]}, "c2")
        self.assertEqual(len(res.images), 1)
        from PIL import Image
        self.assertEqual(Image.open(io.BytesIO(base64.b64decode(res.images[0]["data"]))).size, (400, 300))
        res = box("read_attachment", {"file": image["id"], "region": [5000, 0, 10, 10]}, "c3")
        self.assertTrue(res.content.startswith("Error"))
        # Any bot may read a file the user sent, also from another chat (2026-11-21).
        stranger = self.app.create_bot(name="Stranger", role="other")
        res = Toolbox(self.app, stranger, room=self.app.store.dm_room(stranger["id"]), lang="en")(
            "read_attachment", {"file": long["id"]}, "c4")
        self.assertFalse(res.content.startswith("Error"))

    def test_tool_images_reach_the_api_model_after_the_results(self):
        calls = []

        class LLM:
            def stream_chat(self, messages, **kw):
                from snaplab.llm import Reply
                calls.append(json.loads(json.dumps(messages)))
                if len(calls) == 1:
                    return Reply(content="", tool_calls=[{"id": "t1", "name": "look", "arguments": "{}"}])
                return Reply(content="A red square.")

        def execute(name, args, call_id):
            return ToolResult("map.png follows.", images=[{"mime": "image/jpeg", "data": "QUJD"}])
        res = run_loop(LLM(), [{"role": "user", "content": "look"}], [{"type": "function", "function": {"name": "look"}}],
                       execute, effort="low", max_steps=4, max_tokens=100, source="chat")
        self.assertEqual(res.text, "A red square.")
        roles = [m["role"] for m in calls[1]]
        self.assertEqual(roles[-2:], ["tool", "user"])
        self.assertEqual(calls[1][-1]["content"][1]["image_url"]["url"], "data:image/jpeg;base64,QUJD")

    def test_files_go_to_a_session_workspace(self):
        image = self.sent("ref.png", photo(32, 32), "image/png")
        private = self.sent("me.txt", f"home: {self.app.redactor.home}\\x".encode())
        with tempfile.TemporaryDirectory() as folder:
            note = uploads.give_to_session(self.app, Path(folder), [image["id"], private["id"], image["id"]])
            self.assertIn("- from_chat/ref.png (image 32×32", note)
            self.assertIn("private words were replaced", note)
            self.assertEqual((Path(folder) / "from_chat/ref.png").read_bytes(), photo(32, 32))
            self.assertNotIn(self.app.redactor.home, (Path(folder) / "from_chat/me.txt").read_text(encoding="utf-8"))
            again = uploads.give_to_session(self.app, Path(folder), [image["id"]])
            self.assertIn("from_chat/ref (2).png", again)
        with self.assertRaises(ValueError):
            uploads.prepare(self.app, ["f-" + "0" * 32])
        self.app.guard.add("sk-test-SECRET-VALUE-123456")
        leak = self.sent("keys.txt", b"key=sk-test-SECRET-VALUE-123456")
        with self.assertRaises(ValueError):
            uploads.prepare(self.app, [leak["id"]])

    def test_page_policy_allows_previews_and_the_camera(self):
        status, _, headers = self.req("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("img-src 'self' data: blob:;", headers["Content-Security-Policy"])   # picked photos, before sending
        self.assertIn("camera=(self)", headers["Permissions-Policy"])

    @unittest.skipUnless(__import__("shutil").which("git"), "git is not installed")
    def test_japanese_file_names_come_back_from_git_as_they_are(self):
        from unittest.mock import patch
        from snaplab import gitops
        with tempfile.TemporaryDirectory() as folder, patch.object(gitops, "available", return_value=True):
            root = Path(folder)
            gitops.run(root, "init", "-q")
            for k, v in (("user.name", "t"), ("user.email", "t@example.com")):
                gitops.run(root, "config", k, v)
            (root / "a.txt").write_text("a", encoding="utf-8")
            gitops.run(root, "add", "-A")
            gitops.run(root, "commit", "-qm", "base")
            base = gitops.run(root, "rev-parse", "HEAD").stdout.strip()
            (root / "from_chat").mkdir()
            (root / "from_chat" / "買い物.csv").write_text("x", encoding="utf-8")
            gitops.run(root, "add", "-A")
            gitops.run(root, "commit", "-qm", "files")
            self.assertEqual(gitops.diff_summary(root, base)["changes"], [{"path": "from_chat/買い物.csv", "change": "added"}])

    def test_bot_tool_passes_files_to_start_and_send(self):
        project = self.app.create_project(name="Shop", description="A web shop")
        image = self.sent("logo.png", photo(40, 40), "image/png")
        room = self.app.store.room(self.room["id"])
        box = Toolbox(self.app, self.bot, room=room, lang="en")
        seen = {}
        original_create, original_send = self.app.sessions.create, self.app.sessions.send
        self.app.sessions.create = lambda *a, **k: seen.setdefault("create", k) and {"id": "s-00000001", "status": "queued"}
        self.app.sessions.send = lambda *a, **k: seen.setdefault("send", k)
        self.app.cfg.setdefault("approvals", {}).update({"start_session": "never", "send_to_session": "never"})
        try:
            res = box("start_session", {"project": project["id"], "prompt": "Use the logo", "files": [image["id"]]}, "s1")
            self.assertEqual(seen["create"]["files"], [image["id"]], res.content)
            res = box("start_session", {"project": project["id"], "prompt": "x", "files": ["../etc/passwd"]}, "s2")
            self.assertTrue(res.content.startswith("Error"))
        finally:
            self.app.sessions.create, self.app.sessions.send = original_create, original_send


if __name__ == "__main__":
    unittest.main()
