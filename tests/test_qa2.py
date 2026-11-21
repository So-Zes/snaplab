"""QA round 2 (2026-11-01): the IPv6 hole in read_page (H-2), language of the bots and of files (H-1), and the
smaller findings fixed after it."""
import unittest

from helpers import make_app, onboard, wait_until

from snaplab import websearch

OWN = "2400:4050:a1b2:c3d4:1111:2222:3333:4444"     # pretend public IPv6 address of this PC


class OwnAddressTest(unittest.TestCase):
    def setUp(self):
        websearch.set_local_addresses({"192.168.1.20", OWN, "fe80::1"})

    def tearDown(self):
        websearch.set_local_addresses(None)

    def test_the_pc_and_its_ipv6_network_are_not_public(self):
        self.assertFalse(websearch.ip_is_public(OWN))
        self.assertFalse(websearch.ip_is_public("2400:4050:a1b2:c3d4::1"))           # the router in the same /64
        self.assertTrue(websearch.ip_is_public("2400:4050:a1b2:9999::1"))            # someone else's network
        self.assertTrue(websearch.ip_is_public("2001:4860:4860::8888"))

    def test_ipv4_inside_ipv6_is_unwrapped(self):
        for a in ("::ffff:127.0.0.1", "::7f00:1", "64:ff9b::7f00:1", "64:ff9b::c0a8:114", "64:ff9b:1::a00:1",
                  "2002:7f00:1::1", "2002:c0a8:114::1", "fec0::1"):
            self.assertFalse(websearch.ip_is_public(a), a)
        self.assertTrue(websearch.ip_is_public("64:ff9b::808:808"))                   # NAT64 of a public address

    def test_read_page_refuses_own_address_and_redirects_to_it(self):
        for url in (f"http://[{OWN}]:5357/", "http://[2400:4050:a1b2:c3d4::1]/", "http://[::7f00:1]:5357/"):
            with self.assertRaises(websearch.SearchError, msg=url) as cm:
                websearch.read_page(url)
            self.assertEqual(cm.exception.kind, "blocked")
        orig = websearch._http
        websearch._http = lambda url, **k: (302, {"Location": f"http://[{OWN}]:5357/"}, b"", url)
        try:
            with self.assertRaises(websearch.SearchError) as cm:
                websearch.read_page("http://evil.example/", resolver=lambda h: h == "evil.example")
            self.assertEqual(cm.exception.kind, "blocked")                              # every hop is checked again
        finally:
            websearch._http = orig

    def test_the_connected_peer_is_checked_too(self):
        class Sock:
            def getpeername(self):
                return (OWN, 80, 0, 0)

            def close(self):
                pass
        with self.assertRaises(websearch.SearchError):
            websearch._check_peer(Sock())

    def test_real_lookup_finds_this_pc(self):
        websearch.set_local_addresses(None)
        addrs, _ = websearch.local_addresses()
        self.assertTrue(addrs)
        for a in addrs:
            self.assertFalse(websearch.ip_is_public(str(a)), str(a))


class LanguageTest(unittest.TestCase):
    def test_a_japanese_reply_in_an_english_room_is_rewritten_once(self):
        from test_core import script
        app, llm, _ = make_app(script({"content": "できました。プロジェクトを作りました。"}, {"content": "Done. I created the project."}))
        room = onboard(app)["room"]["id"]
        app.chat.post(room, "Create a project please")
        self.assertTrue(wait_until(lambda: app.store.messages(room)[-1].get("status") == "done"
                                   and app.store.messages(room)[-1].get("role") == "bot", 5))
        self.assertTrue(wait_until(lambda: app.store.messages(room)[-1].get("text") == "Done. I created the project.", 5))
        self.assertIn("Write your whole reply again in English", llm.calls[1]["messages"][-1]["content"])
        sys_prompt = llm.calls[0]["messages"][0]["content"]
        self.assertIn("- Language: reply, and write files", sys_prompt)
        self.assertIn("Reply in English", llm.calls[0]["messages"][-1]["content"])

    def test_japanese_room_is_left_alone_and_user_language_wins(self):
        from snaplab import prompts
        self.assertFalse(prompts.wrong_language("できました。", "ja", "作って"))
        self.assertFalse(prompts.wrong_language("できました。", "en", "日本語で答えて"))
        self.assertTrue(prompts.wrong_language("できました。プロジェクトを作りました。", "en", "Please create it"))

    def test_brief_in_another_language_is_accepted_and_the_template_follows_the_app(self):
        # Tool arguments in another language are no longer refused (2026-11-21); replies still follow the room.
        from snaplab.tools import Toolbox
        app, _, _ = make_app()
        room = onboard(app)["room"]["id"]
        p = app.create_project(name="Word stats", description="d")
        app.chat.post(room, "Write the brief")
        box = Toolbox(app, app.store.orchestrator(), room=app.store.room(room), lang="en")
        res = box("write_brief", {"project": p["id"], "text": "# Word stats\n\n## 目標\n単語を数える小さなモジュール。"}, "c1")
        self.assertNotIn("Error", res.content)
        self.assertIn("単語を数える", app.read_brief(p["id"]))
        res = box("assign_project", {"project": p["id"], "bot": "Marlin", "note": "BRIEF を読んで進めてください。"}, "c2")
        self.assertNotIn("write note in English", res.content)
        app2, _, _ = make_app()
        onboard(app2, lang="ja", name="ミナト")
        p2 = app2.create_project(name="文字数カウンタ", description="文字を数える")
        text = app2.read_brief(p2["id"])
        self.assertIn("## 目的", text)
        self.assertEqual(p2["slug"], "文字数カウンタ")


if __name__ == "__main__":
    unittest.main()
