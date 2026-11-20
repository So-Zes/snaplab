"""Faces, upload boundaries, paired delivery, and icon settings; temporary data only."""
import base64
import io
import json
import struct
import tempfile
import unittest
import zlib
from contextlib import ExitStack
from pathlib import Path

from helpers import make_app, onboard, OWNER
from snaplab import avatars, icons, server
from snaplab.store import Store

ROOT = Path(__file__).resolve().parent.parent


def png(width=256, height=256):
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\0" + b"\xff\x80\x40\xff" * width) * height)) + chunk(b"IEND", b""))


class AvatarTest(unittest.TestCase):
    def test_catalog_and_all_images(self):
        catalog = avatars.catalog()
        self.assertEqual(len(catalog["avatars"]), 34)
        self.assertEqual(set(catalog["sets"]), set("abcde"))
        for face in catalog["avatars"]:
            self.assertEqual(set(face["name"]), {"en", "ja", "ru"})
            for size, filename in face["files"].items():
                with self.subTest(filename=filename):
                    raw = (ROOT / "web/avatars" / filename).read_bytes()
                    self.assertEqual(avatars.image_info(raw), ("png", int(size), int(size)))
                    self.assertIn("/avatars/" + filename, server.STATIC)

    def test_defaults_changes_and_old_bot_fallback(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as cleanup:
            app, _, _ = make_app(tmp=tmp)
            cleanup.callback(app.archive.close)
            lead = onboard(app)["bot"]
            self.assertEqual(lead["avatar"], "c1")
            bot = app.create_bot(name="Builder", role="Code")
            self.assertEqual(bot["avatar"], "c4")
            self.assertEqual(app.create_bot(name="Writer", role="文章")["avatar"], "c5")
            app.update_bot(bot["id"], {"avatar": "e8"})
            self.assertEqual(app.store.bot(bot["id"])["avatar"], "e8")
            app.update_bot(bot["id"], {"avatar": ""})
            self.assertEqual(app.store.bot(bot["id"])["avatar"], "")
            old = app.store.bot(bot["id"])
            del old["avatar"]
            app.store.save_bot(old)
            self.assertNotIn("avatar", app.update_bot(bot["id"], {"role": "Review"}))
            self.assertEqual(app.create_bot(name="Initial", avatar="")["avatar"], "")
            for bad in ("../../config.json", "https://example.org/a.png", "a99", None, {}):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    app.update_bot(bot["id"], {"avatar": bad})

    def test_formats_and_content_not_extension(self):
        self.assertEqual(avatars.image_info(png()), ("png", 256, 256))
        for ext in ("jpg", "webp"):
            raw = (ROOT / "tests/fixtures" / ("avatar." + ext)).read_bytes()
            self.assertEqual(avatars.image_info(raw), (ext, 16, 12))
            with self.assertRaises(ValueError):
                avatars.image_info(raw[:-1])

    def test_invalid_size_dimensions_and_container(self):
        raw = png()
        bad_images = [b"", b"GIF89a", b"<svg></svg>", b"x" * (avatars.MAX_BYTES + 1),
                      png(0, 1), png(1025, 1), raw[:24], raw[:-1], raw + b"extra",
                      raw[:50] + bytes([raw[50] ^ 1]) + raw[51:], b"\xff\xd8\xff\xd9"]
        for raw in bad_images:
            with self.subTest(length=len(raw)), self.assertRaises(ValueError):
                avatars.image_info(raw)
        jpeg = bytearray((ROOT / "tests/fixtures/avatar.jpg").read_bytes())
        frame = jpeg.index(b"\xff\xc0")
        jpeg[frame + 7:frame + 9] = (1025).to_bytes(2, "big")
        with self.assertRaises(ValueError):
            avatars.image_info(jpeg)
        webp = bytearray((ROOT / "tests/fixtures/avatar.webp").read_bytes())
        frame = webp.index(b"\x9d\x01\x2a")
        webp[frame + 3:frame + 5] = (1025).to_bytes(2, "little")
        with self.assertRaises(ValueError):
            avatars.image_info(webp)

    def test_upload_persistence_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            encoded = base64.b64encode(png()).decode()
            name = avatars.save_upload(store, encoded)
            self.assertRegex(name, avatars.UPLOAD_RE)
            self.assertEqual(store.path("avatars", name).read_bytes(), png())
            self.assertEqual(avatars.save_upload(store, encoded), name)
            self.assertEqual(avatars.validate_choice(name, Store(tmp)), name)
            self.assertEqual(len(list(store.path("avatars").iterdir())), 1)
            for bad in (None, [], "!!!!", "a" * 1_400_000):
                with self.assertRaises(ValueError):
                    avatars.save_upload(store, bad)
            with self.assertRaises(ValueError):
                avatars.validate_choice("upload-" + "0" * 64 + ".png", store)

    def test_both_icon_sets_and_sprite(self):
        names = icons.names(ROOT / "web/icons/ui")
        self.assertEqual(len(names), 58)
        for name in names:
            for folder in ("", "dark/"):
                file = f"icons/soft3d/{folder}{name}.png"
                self.assertTrue((ROOT / "web" / file).is_file(), file)
                self.assertIn("/" + file, server.STATIC)
        sprite = icons.sprite(ROOT / "web/icons/ui")
        for name in ("approve", "person-add", "users-add", "progress", "connection", "review", "pc"):
            self.assertIn(f'id="i-{name}"', sprite)


class AvatarRouteTest(unittest.TestCase):
    @classmethod
    def req(cls, method, path, body=None, *, user=OWNER, cookie=True, xhdr=True, origin=None, extra_headers=None):
        # Run the real HTTP parser, gates and handler without opening a socket.
        headers = {"Host": "test.local", "Connection": "close"}
        if user:
            headers["Tailscale-User-Login"] = user
        if cookie and cls.cookie:
            headers["Cookie"] = cls.cookie
        if origin:
            headers["Origin"] = origin
        data = json.dumps(body).encode() if body is not None else b""
        if body is not None:
            headers.update({"Content-Type": "application/json", "Content-Length": str(len(data))})
            if xhdr:
                headers["X-SnapLab"] = "1"
        headers.update(extra_headers or {})
        request = (f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n").encode() + data

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

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.app, _, _ = make_app(tmp=cls.tmp.name)
        cls.addClassCleanup(cls.app.archive.close)
        cls.app.public_url = "http://test.local"
        token, _ = cls.app.auth.new_pairing("test")
        cls.cookie = None
        cls.cookie = cls.req("POST", "/api/pair", {"token": token}, cookie=False)[2]["Set-Cookie"].split(";")[0]
        onboard(cls.app)

    def test_upload_gate_mime_cache_and_bot_assignment(self):
        body = {"image": base64.b64encode(png()).decode()}
        self.assertEqual(self.req("POST", "/api/avatars", body, cookie=False)[0], 401)
        self.assertEqual(self.req("POST", "/api/avatars", body, user=None)[0], 403)
        self.assertEqual(self.req("POST", "/api/avatars", body, xhdr=False)[0], 403)
        self.assertEqual(self.req("POST", "/api/avatars", body, origin="https://evil.example")[0], 403)
        status, raw, _ = self.req("POST", "/api/avatars", body)
        self.assertEqual(status, 200)
        name = json.loads(raw)["avatar"]
        url = "/api/avatars/" + name
        self.assertEqual(self.req("GET", url, cookie=False)[0], 401)
        self.assertEqual(self.req("GET", url, user=None)[0], 403)
        status, raw, headers = self.req("GET", url)
        self.assertEqual((status, raw), (200, png()))
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        lead = self.app.store.orchestrator()
        self.assertEqual(self.req("POST", "/api/bots/" + lead["id"], {"avatar": name})[0], 200)
        self.assertEqual(self.app.store.bot(lead["id"])["avatar"], name)
        # Revocation takes effect even when the device has previously loaded the image.
        token, _ = self.app.auth.new_pairing("temporary device")
        _, raw, headers = self.req("POST", "/api/pair", {"token": token}, cookie=False)
        self.app.auth.revoke(json.loads(raw)["device"]["id"])
        original = self.__class__.cookie
        try:
            self.__class__.cookie = headers["Set-Cookie"].split(";")[0]
            self.assertEqual(self.req("GET", url)[0], 401)
        finally:
            self.__class__.cookie = original

    def test_reject_bad_upload_and_paths(self):
        self.assertEqual(self.req("POST", "/api/avatars", {"image": "<svg>"})[0], 400)
        self.assertEqual(self.req("POST", "/api/avatars", {"image": "a" * 1_400_001})[0], 413)
        for path in ("/api/avatars/../../settings.json", "/api/avatars/upload-" + "0" * 64 + ".png", "/avatars/../app.py"):
            self.assertEqual(self.req("GET", path)[0], 404)
        self.assertEqual(self.req("GET", "/avatars/avatars.json", cookie=False)[0], 401)
        self.assertEqual(self.req("GET", "/avatars/c/c1-96.png")[0], 200)

    def test_icon_setting_default_persists_and_rejects_unknown(self):
        self.assertEqual(Store(self.tmp.name + "/fresh").settings()["icon_style"], "line")
        for style in ("soft3d", "line"):
            status, raw, _ = self.req("POST", "/api/settings", {"icon_style": style})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw)["settings"]["icon_style"], style)
            self.assertEqual(Store(self.app.store.dir).settings()["icon_style"], style)
        self.assertEqual(self.req("POST", "/api/settings", {"icon_style": "unknown"})[0], 400)


if __name__ == "__main__":
    unittest.main()
