"""Round 6: the raw archive, recall, and what each bot may see."""
import re
import shutil
import tempfile
import json
import unittest

from helpers import make_app, onboard, wait_until

from snaplab.archive import Scope


class ArchiveTest(unittest.TestCase):
    def _app(self, responder=None, **over):
        app, llm, _ = make_app(responder, **over)
        onboard(app)
        return app, llm

    def test_messages_are_kept_once_and_found_again(self):
        app, _ = self._app(lambda m, t, e: {"content": "We chose pytest for the calculator tests."})
        room = app.store.dm_room(app.store.orchestrator()["id"])["id"]
        app.chat.post(room, "電卓のテストはどうする？")
        self.assertTrue(wait_until(lambda: app.store.messages(room)[-1].get("status") == "done"))
        hits = app.archive.search("pytest calculator", Scope(everything=True))
        self.assertTrue(any("pytest" in h["text"] for h in hits))
        self.assertTrue(app.archive.search("電卓", Scope(everything=True)))        # 2 characters: LIKE
        self.assertTrue(app.archive.search("テスト", Scope(everything=True)))      # 3 characters: trigram
        n = app.archive.stats()["records"]
        m = app.store.messages(room)[-1]
        app.store.add_message(room, dict(m), touch_room=False)                     # saved again: not archived twice
        self.assertEqual(app.archive.stats()["records"], n)
        self.assertTrue(list(app.store.path("archive").glob("*/*.jsonl")))

    def test_tool_calls_and_whole_pages_are_kept(self):
        long_page = {"url": "https://example.org/long", "title": "Long", "text": "start " * 10, "truncated": True,
                     "content_type": "text/html", "full_text": "start " + "middle " * 5000 + "THE-END-MARK"}
        app, _ = self._app()
        app._page_reader = lambda url: long_page
        from snaplab.tools import Toolbox
        lead = app.store.orchestrator()
        room = app.store.dm_room(lead["id"])
        box = Toolbox(app, lead, room=room, lang="en")
        res = box("read_page", {"url": "https://example.org/long"}, "c1")
        self.assertNotIn("THE-END-MARK", res.content)                 # the model gets the usual size
        hits = app.archive.search("THE-END-MARK", Scope(everything=True))
        self.assertEqual([h["kind"] for h in hits], ["page"])         # the archive keeps the whole page
        box("list_projects", {}, "c2")
        self.assertTrue(app.archive.search("list_projects", Scope(everything=True), kinds=["tool"]))

    def test_a_teammate_finds_only_what_it_may_see(self):
        from snaplab.tools import Toolbox
        app, _ = self._app()
        p = app.create_project(name="Calc")
        q = app.create_project(name="Secret plan")
        ken = app.create_bot(name="Ken", projects=[p["id"]])
        lead = app.store.orchestrator()
        lead_room = app.store.dm_room(lead["id"])["id"]
        app.store.add_message(lead_room, {"role": "user", "text": "the zebra password idea is private"})
        ken_room = app.store.dm_room(ken["id"])["id"]
        app.store.add_message(ken_room, {"role": "user", "text": "zebra stripes in the calculator theme"})
        kbox = Toolbox(app, app.store.bot(ken["id"]), room=app.store.room(ken_room), lang="en")
        res = kbox("recall", {"query": "zebra"}, "c1")
        self.assertIn("stripes", res.content)
        self.assertNotIn("password", res.content)
        lbox = Toolbox(app, lead, room=app.store.room(lead_room), lang="en")
        res = lbox("recall", {"query": "zebra"}, "c2")
        self.assertIn("stripes", res.content)
        self.assertIn("password", res.content)
        rid = re.search(r"\[(a-[0-9a-f]{8})\]", lbox("recall", {"query": "password"}, "c3").content).group(1)
        self.assertIn("Error", kbox("recall", {"record": rid}, "c4").content)    # not by its id either
        self.assertNotIn("Secret plan", kbox("list_projects", {}, "c5").content)
        res = kbox("start_session", {"project": q["id"], "harness": "bot", "prompt": "look"}, "c6")
        self.assertIn("only the owner or lead bot", res.content)
        self.assertTrue(json.loads(kbox("create_project", {"name": "X", "description": "d"}, "c7").content)["created"])
        self.assertIn("Ken", lbox("read_memory", {"bot": "Ken"}, "c8").content)
        self.assertNotIn("read_memory", [t["function"]["name"] for t in kbox.schemas()])

    def test_forgotten_memory_is_still_in_the_record(self):
        app, _ = self._app()
        lead = app.store.orchestrator()
        app.memory_write(lead["id"], "Sam likes green tea")
        app.memory_write(lead["id"], "green tea", mode="forget")
        self.assertNotIn("green tea", app.store.memory(lead["id"]))
        self.assertTrue(app.archive.search("green tea", Scope(everything=True), kinds=["memory"]))

    def test_web_material_found_by_recall_counts_as_web(self):
        from snaplab.tools import Toolbox
        app, _ = self._app()
        lead = app.store.orchestrator()
        room = app.store.dm_room(lead["id"])
        box = Toolbox(app, lead, room=room, lang="en")
        box("read_page", {"url": "https://example.org/a"}, "c1")
        box2 = Toolbox(app, lead, room=room, lang="en")
        res = box2("recall", {"query": "deletes files"}, "c2")
        self.assertTrue(box2.read_web)
        self.assertIn("untrusted", res.content)

    def test_rerank_asks_the_model_behind_chats(self):
        seen = []

        def responder(messages, tools, effort):
            if "You pick search results" in messages[0]["content"]:
                seen.append(1)
                return {"content": "3, 1"}
            return {"content": "ok"}
        app, llm = self._app(responder, recall={"results": 2, "rerank": True})
        lead = app.store.orchestrator()
        room = app.store.dm_room(lead["id"])["id"]
        for i in range(4):
            app.store.add_message(room, {"role": "user", "text": f"note {i} about the kiwi harvest"})
        hits = app.archive.search("kiwi", Scope(everything=True))
        out = app.archive.rerank("kiwi", hits, 2)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["id"], hits[2]["id"])
        self.assertTrue(seen)
        self.assertEqual([c["kind"] for c in llm.calls if c["source"] == "recall"], ["followup"])

    def test_an_older_folder_is_filled_into_the_archive_once(self):
        tmp = tempfile.mkdtemp(prefix="snaplab-back-")
        app, _, _ = make_app(tmp=tmp)
        onboard(app)
        room = app.store.dm_room(app.store.orchestrator()["id"])["id"]
        app.store.add_message(room, {"role": "user", "text": "an old message about lighthouses"})
        app.archive.close()
        shutil.rmtree(app.store.path("archive"))
        app.store.path("schema.json").unlink()                 # as if written by round 5
        app2, _, _ = make_app(tmp=tmp)
        self.assertTrue(app2.archive.search("lighthouses", Scope(everything=True)))
        n = app2.archive.stats()["records"]
        app2.archive.close()
        app3, _, _ = make_app(tmp=tmp)                         # no second fill
        self.assertEqual(app3.archive.stats()["records"], n)

    def test_index_is_rebuilt_from_the_files(self):
        tmp = tempfile.mkdtemp(prefix="snaplab-idx-")
        app, _, _ = make_app(tmp=tmp)
        onboard(app)
        room = app.store.dm_room(app.store.orchestrator()["id"])["id"]
        app.store.add_message(room, {"role": "user", "text": "remember the walrus"})
        app.archive.close()
        for name in ("index.sqlite3", "index.sqlite3-wal", "index.sqlite3-shm"):
            try:
                app.store.path("archive", name).unlink()
            except FileNotFoundError:
                pass
        app2, _, _ = make_app(tmp=tmp)
        self.assertTrue(app2.archive.search("walrus", Scope(everything=True)))


if __name__ == "__main__":
    unittest.main()
