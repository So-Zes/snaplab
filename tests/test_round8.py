"""Round 8: isolated provenance, temporal recall, state, compression and evaluation tests."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import make_app, onboard
from snaplab.archive import Archive, Scope
from snaplab.agent import ToolResult, _compact, run_loop
from snaplab.evidence import artifact_version
from snaplab.ledger import test_evidence
from snaplab.llm import FakeLLM
from snaplab.temporal import digest
from snaplab.tools import Toolbox
from snaplab import summary_schema

OLD = "2026-10-01T00:00:00+00:00"
NEW = "2026-10-20T00:00:00+00:00"
MID = "2026-10-10T00:00:00+00:00"
LATE = "2026-10-25T00:00:00+00:00"


class ContextTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app, self.llm, _ = make_app(tmp=self.tmp.name, summary={"enabled": False}, recall={"rerank": False})
        self.addCleanup(self.app.archive.close)
        onboard(self.app)
        self.bot = self.app.store.orchestrator()
        self.room = self.app.store.dm_room(self.bot["id"])
        self.scope = Scope(everything=True)

    def rec(self, text, key=None, stamp=OLD, **meta):
        return self.app.archive.add("memory", text, key=key, room=self.room["id"], ts=stamp,
            meta={"recorded_at": stamp, "valid_from": stamp, "source_type": "user", **meta})

    def search(self, query, **kw):
        return self.app.archive.search(query, self.scope, mode=kw.pop("mode", "current"),
                                       as_of=kw.pop("as_of", LATE), known_at=kw.pop("known_at", LATE), **kw)

    def work(self, **changes):
        s = {"id": "s-00000001", "project": "p-00000001", "status": "review", "title": "Build sail",
             "harness": "bot", "turns": 1, "artifact_version": "rev2", "last_prompt": "Build a sail"}
        s.update(changes)
        return self.app.store.save_session(s)

    def test_current_and_historical_and_original_are_distinct(self):
        old = self.rec("owner Alice", "owner")
        new = self.rec("owner Bob", "owner", NEW)
        self.assertEqual([r["id"] for r in self.search("owner")], [new["id"]])
        self.assertEqual([r["id"] for r in self.search("owner", mode="historical", as_of=MID)], [old["id"]])
        self.assertEqual(self.app.archive.get(old["id"], self.scope)["text"], "owner Alice")
        originals = [r for f in self.app.archive.dir.glob("*/*.jsonl") for r in self.app.store.read_jsonl(f)]
        self.assertIsNone(next(r for r in originals if r["id"] == old["id"])["meta"].get("valid_to"))

    def test_revert_same_text_creates_a_new_version(self):
        self.rec("owner Alice", "owner")
        self.rec("owner Bob", "owner", NEW)
        third = self.rec("owner Alice", "owner", LATE)
        self.assertIsNotNone(third)
        self.assertEqual(third["meta"]["sequence"], 3)
        self.assertEqual([r["id"] for r in self.search("owner")], [third["id"]])

    def test_recorded_time_does_not_replace_effective_time(self):
        old = self.rec("owner Alice", "owner")
        self.rec("owner Bob", "owner", NEW, valid_from="2026-10-09T00:00:00Z")
        self.assertEqual(self.search("owner", as_of=MID)[0]["text"], "owner Bob")
        self.assertEqual(self.search("owner", as_of=MID, known_at=MID)[0]["id"], old["id"])

    def test_aliases_short_words_and_filter_before_limit(self):
        for i in range(30):
            self.rec(f"migration choice {i}", "choice", f"2026-10-{i%28+1:02}T00:00:00+00:00")
        final = self.rec("new choice", "choice", LATE, aliases=["旧称の灯台", "帆"], entity="p-00000001")
        self.assertEqual(self.search("帆", limit=1)[0]["id"], final["id"])
        self.assertEqual(self.search("旧称の灯台", limit=1)[0]["id"], final["id"])

    def test_index_rebuild_keeps_time_and_aliases(self):
        self.rec("owner Alice", "owner")
        latest = self.rec("owner Bob", "owner", NEW, aliases=["航路"])
        self.app.archive.close()
        for name in ("index.sqlite3", "index.sqlite3-wal", "index.sqlite3-shm"):
            self.app.store.path("archive", name).unlink(missing_ok=True)
        self.app.archive = Archive(self.app)
        self.addCleanup(self.app.archive.close)
        self.assertEqual(self.search("航路")[0]["id"], latest["id"])
        self.assertEqual(len(self.search("owner")), 1)

    def test_temporal_input_is_validated(self):
        with self.assertRaises(ValueError):
            self.search("owner", mode="historical", as_of=None)
        with self.assertRaises(ValueError):
            self.search("owner", as_of="2026-10-10")

    def test_archive_metadata_is_guarded_before_indexing(self):
        marker = "synthetic-private-marker"
        with patch.object(self.app.guard, "clean", side_effect=lambda value: value.replace(marker, "[removed]")):
            rec = self.rec("safe text", entity_name=marker, aliases=[marker], sources=[marker])
        self.assertNotIn(marker, json.dumps(rec))
        self.assertFalse(self.app.archive.search(marker, self.scope))
        self.assertNotIn(marker, self.app.store.path("archive", "index.sqlite3").read_bytes().decode("latin1"))

    def test_provenance_and_untrusted_status_survive_recall(self):
        source = self.app.archive.add("page", "owner Bob", room=self.room["id"], meta={"untrusted": True})
        rec = self.app.facts.add(bot=self.bot, entity="boat", field="owner", text="owner Bob",
                                 sources=[source["id"]], aliases=["sailboat"])
        box = Toolbox(self.app, self.bot, room=self.room)
        result = box("recall", {"query": "sailboat"}, "call")
        self.assertTrue(box.read_web)
        self.assertIn(source["id"], result.content)
        self.assertIn("untrusted", result.content)
        self.assertEqual(rec["meta"]["sources"][0]["range"], [0, 9])

    def test_fact_replacement_cannot_widen_acl_or_promote_web(self):
        source = self.app.archive.add("page", "owner Bob", room=self.room["id"])
        with self.assertRaises(ValueError):
            self.app.facts.add(bot=self.bot, entity="boat", field="owner", text="Bob", sources=[source["id"]], status="verified")
        with self.assertRaises(ValueError):
            self.app.facts.add(bot=self.bot, entity="boat", field="owner", text="Bob", sources=[source["id"]], shared=True)

    def test_verified_fact_cannot_be_replaced_by_a_conjecture(self):
        source = self.rec("owner Alice")
        old = self.app.facts.add(bot=self.bot, entity="boat", field="owner", text="Alice", sources=[source["id"]],
                                 status="verified", valid_from=OLD)
        with self.assertRaises(ValueError):
            self.app.facts.add(bot=self.bot, entity="boat", field="owner", text="Bob", sources=[source["id"]],
                               supersedes=[old["id"]], valid_from=NEW)

    def test_live_scope_rechecks_project_ownership(self):
        teammate = self.app.create_bot(name="Reader")
        teammate["projects"] = ["p-00000001"]
        self.app.store.save_bot(teammate)
        rec = self.app.archive.add("doc", "private reef", project="p-00000001")
        stale_bot = copy.deepcopy(teammate)
        teammate["projects"] = []
        self.app.store.save_bot(teammate)
        box = Toolbox(self.app, stale_bot, room=self.app.store.dm_room(teammate["id"]))
        self.assertIn("Error", box("recall", {"record": rec["id"]}, "call").content)
        self.assertFalse(self.app.archive.search("reef", Scope.for_bot(self.app, stale_bot)))

    def test_memory_forget_preserves_history_but_not_current_snapshot(self):
        self.app.memory_write(self.bot["id"], "likes green tea")
        self.app.memory_write(self.bot["id"], "green tea", mode="forget")
        self.assertFalse(self.app.archive.search("green tea", self.scope, kinds=["memory"], mode="current"))
        self.assertTrue(self.app.archive.search("green tea", self.scope, kinds=["memory"], mode="all"))

    def test_ledger_uses_runtime_states_and_tested_revision(self):
        s = self.work(tests={"status": "passed", "tested_version": "rev1", "version_after": "rev1", "exit_code": 0,
                             "source_type": "runtime"},
                      last_message="Everything passed!")
        item = next(r for r in self.app.ledger.concise(self.bot)["items"] if r["id"] == s["id"])
        self.assertEqual(item["tests"]["current_status"], "unverified")
        self.assertFalse(item["worker_report_is_evidence"])
        self.assertTrue(self.app.archive.get(item["source"], self.scope))
        s["tests"].update(tested_version="rev2", version_after="rev2")
        self.app.store.save_session(s)
        self.assertTrue(test_evidence(s)["applies_to_current"])
        s["status"] = "running"
        self.assertFalse(test_evidence(s)["applies_to_current"])

    def test_state_view_is_bounded_and_reports_omissions(self):
        for i in range(20):
            self.app.store.save_session({"id": f"s-{i:08x}", "project": "p-00000001", "status": "failed",
                                         "title": "long " * 15, "plan": {"steps": [{"text": "x" * 200}] * 50}})
        view = self.app.ledger.concise(self.bot, limit=8, token_budget=500)
        self.assertGreater(view["omitted"], 0)
        self.assertEqual(view["counts"]["work"], 20)
        self.assertLess(len(json.dumps(view)), 3000)

    def test_ledger_tracks_tasks_and_tombstones(self):
        t = self.app.tasks.create(bot_id=self.bot["id"], title="check sail", prompt="check", schedule={"kind": "every", "minutes": 30})
        self.app.tasks.change(t["id"], "pause")
        self.app.tasks.change(t["id"], "delete")
        item = next(r for r in self.app.ledger.concise(self.bot)["items"] if r["id"] == t["id"])
        self.assertEqual(item["status"], "deleted")

    def proposal(self, s):
        return self.app.chat.create_question(self.room, self.bot, question="Continue?", kind="approval",
            action={"type": "send_to_session", "session": s["id"], "prompt": "continue", "bot": self.bot["id"]})

    def test_approval_stale_consumed_and_tampered_are_refused(self):
        self.app.store.save_project({"id": "p-00000001", "name": "Sail", "slug": "sail", "bot": self.bot["id"]})
        s = self.work()
        q = self.proposal(s)
        self.assertFalse(self.app.ledger.approval_state(q)["valid"])
        q.update(status="answered", answer="Approve")
        self.app.store.save_question(q)
        s["artifact_version"] = "rev3"
        self.app.store.save_session(s)
        with self.assertRaisesRegex(ValueError, "stale"):
            self.app.ledger.consume_approval(q, True)
        q = self.proposal(s)
        # Only a stored affirmative answer may consume the proposal.
        with self.assertRaises(ValueError):
            self.app.ledger.consume_approval(q, True)
        q.update(status="answered", answer="Approve")
        self.app.store.save_question(q)
        changed = copy.deepcopy(q)
        changed["action"]["prompt"] = "different operation"
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.app.ledger.consume_approval(changed, True)
        self.app.ledger.consume_approval(q, True)
        with self.assertRaises(ValueError):
            self.app.ledger.consume_approval(q, True)

    def test_legacy_and_expired_approvals_are_not_permissions(self):
        q = self.proposal(self.work())
        q.update(status="answered", answer="Approve")
        q["authorization"]["expires_at"] = OLD
        self.app.store.save_question(q)
        with self.assertRaisesRegex(ValueError, "stale"):
            self.app.ledger.consume_approval(q, True)
        q.pop("authorization")
        self.app.store.save_question(q)
        self.assertEqual(self.app.ledger.approval_state(q)["state"], "legacy_unbound")

    def test_prefix_does_not_change_when_ledger_changes(self):
        before, _ = self.app.chat.context.build(self.room, self.bot, "en")
        self.work()
        after, report = self.app.chat.context.build(self.room, self.bot, "en")
        self.assertEqual(before[0], after[0])
        self.assertNotEqual(before[-1], after[-1])
        self.assertIn("s-00000001", after[-1]["content"])
        from snaplab.context import estimate_tokens
        self.assertEqual(report["total"], estimate_tokens(after[0]["content"]) + report["history"])

    def test_tool_original_is_recoverable_and_compaction_preserves_pairing(self):
        self.app._page_reader = lambda _: {"text": "hello " * 1500, "url": "https://example.org", "title": "sail",
                                           "truncated": False, "full_text": "hello " * 1500}
        box = Toolbox(self.app, self.bot, room=self.room)
        result = box("read_page", {"url": "https://example.org"}, "call-1")
        messages = [{"role": "assistant", "tool_calls": [{"id": "call-1"}]},
                    {"role": "tool", "tool_call_id": "call-1", "content": result.content},
                    {"role": "tool", "tool_call_id": "call-2", "content": "error " * 1000}]
        refs = {"call-1": {"record": result.record}, "call-2": {"record": result.record, "protect": True}}
        for _ in range(10):
            _compact(messages, 1, keep_last=0, references=refs)
        self.assertIn(result.record, messages[1]["content"])
        self.assertEqual(messages[1]["tool_call_id"], messages[0]["tool_calls"][0]["id"])
        self.assertGreater(len(messages[2]["content"]), 600)
        reread = box("recall", {"record": result.record, "from_char": 4000}, "read")
        self.assertIn("hello", reread.content)
        self.assertTrue(box.read_web)

    def test_actual_loop_does_not_send_internal_reference_fields(self):
        step = iter([{"tool_calls": [{"name": "read", "arguments": {}}]}] * 4 + [{"content": "done"}])
        llm = FakeLLM(lambda *_: next(step))
        messages = [{"role": "user", "content": "go"}]
        result = run_loop(llm, messages, [{"function": {"name": "read"}}],
                          lambda *_: ToolResult("long " * 1000, record="a-00000001"),
                          effort="low", max_steps=5, max_tokens=100, source="test", compact_at=1)
        self.assertEqual(result.calls, 5)
        self.assertTrue(any("Archived tool result" in m.get("content", "") for m in messages))
        self.assertTrue(all("record" not in m and "protect" not in m for m in messages))
        call_ids = [m["tool_call_id"] for m in messages if m.get("role") == "tool"]
        self.assertEqual(len(call_ids), len(set(call_ids)))

    def test_session_archives_whole_turns_and_retains_recall(self):
        from snaplab.harnesses.bot import retain_history
        s = self.work(goal="Build a sail")
        convo = [{"role": "system", "content": "fixed rules"}, {"role": "user", "content": "first task"},
                 {"role": "assistant", "content": "old details " * 7000},
                 {"role": "user", "content": "continue"}, {"role": "assistant", "content": "new reply"}]
        retain_history(self.app, s, convo, {}, 3)
        note = json.loads(convo[1]["content"].split("\n", 1)[1])
        original = self.app.archive.get(note["coverage"]["record"], self.scope)
        self.assertIn("old details", original["text"])
        self.assertEqual(note["purpose"], "Build a sail")
        self.assertEqual(convo[2]["content"], "continue")
        self.assertEqual(convo[0]["content"], "fixed rules")
        box = Toolbox(self.app, self.bot, session=s)
        self.assertIn("recall", [t["function"]["name"] for t in box.schemas()])
        self.assertIn("old details", box("recall", {"record": original["id"]}, "read").content)

    def test_session_keeps_protected_results_between_turns(self):
        from snaplab.harnesses.bot import retain_history
        s = self.work()
        convo = [{"role": "system", "content": "fixed"}, {"role": "user", "content": "first"},
                 {"role": "assistant", "tool_calls": [{"id": "call"}]},
                 {"role": "tool", "tool_call_id": "call", "content": "error " * 12000},
                 {"role": "user", "content": "continue"}]
        before = copy.deepcopy(convo)
        refs = {"call": {"record": "a-00000001", "protect": True}}
        self.assertEqual(retain_history(self.app, s, convo, refs, 4), refs)
        self.assertEqual(convo, before)

    def summary_reply(self, *_):
        return {"content": json.dumps({k: ["Keep s-00000001 and PLAN.md"] if k == "open_items" else []
                                        for k in summary_schema.FIELDS})}

    def test_ten_summaries_preserve_ids_and_exact_coverage(self):
        self.llm.responder = self.summary_reply
        for i in range(10):
            m = self.app.store.add_message(self.room["id"], {"role": "user", "text": f"note {i}: s-00000001 PLAN.md"})
            self.app.summaries.run(self.room["id"])
            text, meta = self.app.summaries.get(self.room["id"])
            self.assertEqual(meta["through"], m["id"])
            self.assertIn("s-00000001", text)
            self.assertEqual(set(summary_schema.FIELDS), set(meta["structured"]) - {"legacy_text"})
        self.assertEqual(len({m["id"] for m in meta["coverage"]}), len(meta["coverage"]))
        self.assertTrue(all(m["record"] for m in meta["coverage"]))

    def test_summary_does_not_cover_messages_arriving_during_generation(self):
        first = self.app.store.add_message(self.room["id"], {"role": "user", "text": "first"})
        def respond(*args):
            self.app.store.add_message(self.room["id"], {"role": "user", "text": "new arrival"})
            return self.summary_reply()
        self.llm.responder = respond
        self.app.summaries.run(self.room["id"])
        _, meta = self.app.summaries.get(self.room["id"])
        self.assertEqual(meta["through"], first["id"])
        self.assertEqual(self.app.summaries.pending(self.room["id"])[-1]["text"], "new arrival")

    def test_summary_failure_and_concurrent_edit_do_not_advance_coverage(self):
        msg = self.app.store.add_message(self.room["id"], {"role": "user", "text": "first"})
        self.llm.responder = lambda *_: {"content": "not valid"}
        self.app.summaries.run(self.room["id"])
        self.assertEqual(self.app.summaries.get(self.room["id"])[1], {})
        def respond(*_):
            self.app.store.add_message(self.room["id"], {**msg, "text": "corrected"})
            return self.summary_reply()
        self.llm.responder = respond
        self.app.summaries.run(self.room["id"])
        self.assertEqual(self.app.summaries.get(self.room["id"])[1], {})

    def test_covered_message_correction_rebuilds_from_originals(self):
        msg = self.app.store.add_message(self.room["id"], {"role": "user", "text": "first"})
        self.llm.responder = self.summary_reply
        self.app.summaries.run(self.room["id"])
        self.app.store.add_message(self.room["id"], {**msg, "text": "corrected"})
        self.app.summaries.run(self.room["id"])
        _, meta = self.app.summaries.get(self.room["id"])
        covered = next(m for m in meta["coverage"] if m["id"] == msg["id"])
        self.assertEqual(covered["hash"], digest("corrected"))
        self.assertNotIn("first", self.llm.calls[-1]["messages"][-1]["content"])

    def test_migration_is_additive_and_idempotent_on_synthetic_v2(self):
        from snaplab import migrate
        self.work()
        self.app.memory_write(self.bot["id"], "durable note")
        self.app.store.write_json("schema.json", {"version": 2})
        originals = {f: f.read_bytes() for f in self.app.archive.dir.glob("*/*.jsonl")}
        memory = self.app.store.memory(self.bot["id"])
        migrate.run(self.app)
        count = self.app.archive.stats()["records"]
        migrate.run(self.app)
        self.assertEqual(self.app.archive.stats()["records"], count)
        self.assertEqual(self.app.store.memory(self.bot["id"]), memory)
        self.assertTrue(all(f.read_bytes().startswith(before) for f, before in originals.items()))
        self.assertEqual(self.app.store.read_json("schema.json", {})["version"], 3)

    def test_old_sqlite_index_is_upgraded_from_unchanged_originals(self):
        from tools.baselines.round7_archive import FtsIndex as OldIndex
        self.app.archive.close()
        for name in ("index.sqlite3", "index.sqlite3-wal", "index.sqlite3-shm"):
            self.app.store.path("archive", name).unlink(missing_ok=True)
        path = self.app.store.path("archive", "2026-10", "2026-10-01.jsonl")
        records = [{"id": f"a-{i:08x}", "ts": stamp, "kind": "memory", "text": "owner " + name,
                    "key": "memory:legacy", "hash": name, "shared": True, "meta": {}}
                   for i, stamp, name in [(1, OLD, "Alice"), (2, NEW, "Bob")]]
        for record in records:
            self.app.store.append_jsonl(path, record)
        original = path.read_bytes()
        old = OldIndex(self.app.store.path("archive", "index.sqlite3"))
        for record in records:
            old.add(record)
        old.close()
        self.app.archive = Archive(self.app)
        self.addCleanup(self.app.archive.close)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual([h["text"] for h in self.search("owner")], ["owner Bob"])
        self.assertEqual([h["text"] for h in self.search("owner", mode="historical", as_of=MID)], ["owner Alice"])

    def test_conflicting_facts_are_labelled_without_guessing(self):
        self.rec("owner Alice", fact=True, entity="boat", field="owner")
        self.rec("owner Bob", stamp=NEW, fact=True, entity="boat", field="owner")
        hits = self.search("owner")
        self.assertEqual(len(hits), 2)
        self.assertTrue(all(h["meta"]["conflict"] for h in hits))

    def test_summary_oversize_input_is_not_silently_marked_covered(self):
        large = self.app.store.add_message(self.room["id"], {"role": "user", "text": "x " * 50000})
        self.llm.responder = self.summary_reply
        self.app.summaries.run(self.room["id"])
        self.assertNotEqual(self.app.summaries.get(self.room["id"])[1].get("through"), large["id"])
        before = len(self.llm.calls)
        self.app.summaries.run(self.room["id"])
        self.assertEqual(len(self.llm.calls), before)
        self.assertEqual(self.app.summaries.pending(self.room["id"])[0]["id"], large["id"])


class EvidenceTest(unittest.TestCase):
    def test_fingerprint_changes_with_content_not_timestamps(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / "main.py").write_text("one", encoding="utf-8")
            before = artifact_version(p)
            (p / "main.py").touch()
            self.assertEqual(artifact_version(p), before)
            (p / "main.py").write_text("two", encoding="utf-8")
            self.assertNotEqual(artifact_version(p), before)
            self.assertFalse(test_evidence({"artifact_version": "v2", "tests": {"status": "passed"}})["applies_to_current"])


class EvaluationTest(unittest.TestCase):
    def test_corpus_separates_tuning_and_evaluation_and_stub_never_sees_gold(self):
        from tools.build_context_fixture import build
        from tools.context_eval import evaluate, stub
        data = build()
        self.assertEqual(sum(s["split"] == "tune" for s in data["scenes"]), 12)
        self.assertEqual(sum(s["split"] == "eval" for s in data["scenes"]), 120)
        def respond(messages, tools, effort):
            self.assertNotIn('"gold"', messages[-1]["content"])
            return stub(messages, tools, effort)
        for scene in data["scenes"][:12]:
            with self.subTest(family=scene["family"]), tempfile.TemporaryDirectory() as tmp:
                result = evaluate(scene, "candidate", FakeLLM(respond), tmp)
                self.assertTrue(result["score"]["correct"])
                self.assertTrue(result["score"]["citations_correct"])

    def test_wrong_answers_and_unauthorized_actions_fail_scoring(self):
        from tools.context_eval import score
        gold = {"value": "Bob", "sources": ["a-00000001"], "ask": True,
                "action": "request_approval", "stale_values": ["Alice"]}
        result = score({"value": "Alice", "sources": ["a-ffffffff"], "ask": False, "action": "run"}, gold, [])
        self.assertFalse(result["correct"])
        self.assertFalse(result["citations_correct"])
        self.assertFalse(result["action_correct"])
        self.assertTrue(result["stale"])
