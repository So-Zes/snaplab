"""Waiting cards can be closed without answering (2026-11-23): one at a time or the older ones at once. The bot is
not told, an approval's action is not run, and the card leaves the "waiting for you" list."""
import io
import json
import tempfile
import unittest
from unittest.mock import patch

from helpers import make_app, onboard
from snaplab import server


class DismissTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.app, _, _ = make_app(tmp=cls.tmp.name)
        cls.addClassCleanup(cls.app.archive.close)
        cls.app.public_url = "http://test.local"
        token, _ = cls.app.auth.new_pairing("test")
        cls.cookie = None
        cls.cookie = cls.req("POST", "/api/pair", json.dumps({"token": token}).encode(), cookie=False)[2]["Set-Cookie"].split(";")[0]
        r = onboard(cls.app)
        cls.room, cls.bot = cls.app.store.room(r["room"]["id"]), r["bot"]

    @classmethod
    def req(cls, method, path, body=b"", *, cookie=True):
        h = {"Host": "test.local", "Connection": "close", "Tailscale-User-Login": "owner@example.com",
             "Content-Type": "application/json", "X-SnapLab": "1", "Content-Length": str(len(body))}
        if cookie and cls.cookie:
            h["Cookie"] = cls.cookie
        request = (f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items()) + "\r\n").encode() + body

        class Connection:
            output = bytearray()

            def makefile(self, *args):
                return io.BytesIO(request)

            def sendall(self, raw):
                self.output.extend(raw)
        connection = Connection()
        type("TestHandler", (server.Handler,), {"app": cls.app})(connection, ("127.0.0.1", 12345), None)
        head, raw = bytes(connection.output).split(b"\r\n\r\n", 1)
        lines = head.decode().split("\r\n")
        return int(lines[0].split()[1]), raw, dict(line.split(": ", 1) for line in lines[1:])

    def ask(self, text, kind="question"):
        return self.app.chat.create_question(self.room, self.bot, question=text, kind=kind,
                                             action={"type": "message_bot", "bot": self.bot["id"], "to": self.bot["id"], "text": "hi"} if kind == "approval" else None)

    def open_ids(self):
        return [q["id"] for q in self.app.bootstrap()["questions"]]

    def test_closing_one_card_leaves_the_list_and_tells_no_bot(self):
        q = self.ask("Which colour?")
        self.assertIn(q["id"], self.open_ids())
        with patch.object(self.app.chat, "post") as post, patch.object(self.app.bus, "publish") as publish:
            status, raw, _ = self.req("POST", "/api/questions/dismiss", json.dumps({"ids": [q["id"]]}).encode())
        self.assertEqual(status, 200, raw)
        shown = json.loads(raw)["questions"]
        self.assertEqual([(x["id"], x["status"]) for x in shown], [(q["id"], "dismissed")])
        self.assertTrue(shown[0]["dismissed_at"])
        post.assert_not_called()
        self.assertIn("question.updated", [c.args[0] for c in publish.call_args_list])
        self.assertNotIn(q["id"], self.open_ids())
        # closing it again, or a card that does not exist, changes nothing
        status, raw, _ = self.req("POST", "/api/questions/dismiss", json.dumps({"ids": [q["id"], "q-00000000"]}).encode())
        self.assertEqual((status, json.loads(raw)["questions"]), (200, []))
        with self.assertRaises(LookupError):
            self.app.chat.answer(q["id"], "Red")

    def test_an_approval_closed_without_answer_is_not_carried_out(self):
        q = self.ask("Send a message to the team?", kind="approval")
        with patch.object(self.app, "run_approval") as run:
            self.app.chat.dismiss([q["id"]])
        run.assert_not_called()
        saved = self.app.store.question(q["id"])
        self.assertEqual(saved["status"], "dismissed")
        if saved.get("authorization"):
            self.assertEqual(saved["authorization"]["state"], "cancelled")

    def test_several_at_once(self):
        a, b, c = self.ask("A?"), self.ask("B?"), self.ask("C?")
        done = self.app.chat.dismiss([a["id"], b["id"]])
        self.assertEqual({q["id"] for q in done}, {a["id"], b["id"]})
        self.assertIn(c["id"], self.open_ids())
        self.assertNotIn(a["id"], self.open_ids())

    def test_the_route_checks_like_every_other_post(self):
        q = self.ask("Still there?")
        self.assertEqual(self.req("POST", "/api/questions/dismiss", json.dumps({"ids": [q["id"]]}).encode(), cookie=False)[0], 401)
        self.assertIn(q["id"], self.open_ids())


if __name__ == "__main__":
    unittest.main()
