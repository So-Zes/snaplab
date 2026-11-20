"""Memory fixes of 2026-11-21: notes keep their newest lines, budgets follow the model's window, the room summary puts
what comes next first and stops growing, and small windows get a shorter tool list."""
import json
import unittest

from helpers import make_app, onboard
from snaplab import context, summary_schema, tools
from snaplab.context import budgets, clip_newest, estimate_tokens


def note(n, text="今日の打ち合わせで決まった内容をここに書き留めておきます"):
    return "\n".join(f"- [2026-11-{(i % 28) + 1:02d}] {i:03d} {text}" for i in range(n))


class BudgetTest(unittest.TestCase):
    def test_large_window_shows_a_whole_japanese_note(self):
        b = budgets(context.DEFAULTS, 131072, tool_tokens=3600)
        self.assertEqual(b["max_tokens"], 32000)
        self.assertGreaterEqual(b["memory_tokens"], estimate_tokens("あ" * 4000))
        self.assertGreaterEqual(b["user_tokens"], estimate_tokens("あ" * 1400))
        self.assertEqual(b["recent_tokens"], 6000)
        self.assertEqual(b["summarize_tokens"], 24000)

    def test_small_windows_stay_inside_the_provider_check(self):
        for limit in (16384, 32768):
            b = budgets(context.DEFAULTS, limit, tool_tokens=2700)
            out = min(8192, limit // 4)
            # messages + tool list + reply + margin must fit, as provider_protocol.prepare requires
            self.assertLessEqual(b["max_tokens"] + 2700 + out + min(1024, limit // 10), limit)
            self.assertGreater(b["recent_tokens"], 1000)
            self.assertLess(b["summarize_tokens"], b["max_tokens"] + 2700)
            self.assertLess(b["memory_tokens"], b["max_tokens"] * .15)

    def test_config_values_win(self):
        b = budgets({**context.DEFAULTS, "memory_tokens": 800, "user_tokens": 400}, 131072)
        self.assertEqual((b["memory_tokens"], b["user_tokens"]), (800, 400))


class ClipTest(unittest.TestCase):
    def test_newest_lines_stay_and_a_marker_counts_the_rest(self):
        text = note(80)
        out = clip_newest(text, 600)
        self.assertLessEqual(estimate_tokens(out), 600)
        self.assertIn(" 079 ", out)                      # the newest line
        self.assertNotIn(" 000 ", out)                   # the oldest is left out
        hidden = int(out.split("(", 1)[1].split(" ", 1)[0])
        self.assertEqual(hidden + len([x for x in out.splitlines() if x.startswith("- [")]), 80)

    def test_keep_head_and_short_text(self):
        text = "- Wants to be called Rin.\n" + note(60)
        out = clip_newest(text, 400, keep_head=1)
        self.assertTrue(out.startswith("- Wants to be called Rin.\n- ("))
        self.assertIn(" 059 ", out)
        self.assertEqual(clip_newest("short", 100), "short")


class ContextTest(unittest.TestCase):
    def build(self, limit):
        app, _, _ = make_app(None)
        self.addCleanup(app.archive.close)
        r = onboard(app)
        bot = app.store.bot(r["room"]["bots"][0])
        app.cfg["models"][app.models.name_for(None, "chat")]["context_limit"] = limit
        mem =app.store.path("bots", bot["id"], "memory.md")
        mem.write_text(note(70)[:4000], encoding="utf-8")
        builder = context.ContextBuilder(app)
        messages, report = builder.build(app.store.room(r["room"]["id"]), bot, "ja")
        return messages[0]["content"], report

    def test_bot_sees_its_newest_note_at_every_window(self):
        newest = note(70)[:4000].splitlines()[-1]
        for limit in (131072, 32768, 16384):
            system, report = self.build(limit)
            self.assertIn(newest, system, limit)
            self.assertIn("compact_at", report)
        system, _ = self.build(131072)
        self.assertNotIn("older lines are not shown", system)
        system, _ = self.build(16384)
        self.assertIn("older lines are not shown", system)


class SummaryTest(unittest.TestCase):
    def test_render_puts_next_actions_first_and_skips_empty_fields(self):
        data = {k: [] for k in summary_schema.FIELDS}
        data.update(purpose=["Plan the trip"], next_actions=["Book the hotel"], identifiers=["s-0123abcd"])
        text = summary_schema.render(data)
        self.assertLess(text.index("next_actions"), text.index("identifiers"))
        self.assertNotIn("decisions", text)
        clipped = context.clip_tokens(text, 40)
        self.assertIn("Book the hotel", clipped)

    def test_identifiers_are_capped_newest_first(self):
        previous = " ".join(f"p-{i:08x}" for i in range(100))
        kept = summary_schema.keep_identifiers("see s-aaaaaaaa", ["p-00000001"], previous, "p-00000002 is still open")
        self.assertEqual(kept[:3], ["s-aaaaaaaa", "p-00000001", "p-00000002"])
        self.assertEqual(len(kept), 3)
        many = summary_schema.keep_identifiers(previous, [], "", "")
        self.assertEqual(len(many), summary_schema.MAX_IDENTIFIERS)


class SchemaTest(unittest.TestCase):
    def test_compact_schemas_keep_names_types_and_required(self):
        full = tools.CHAT_TOOLS
        small = tools.compact_schemas(full)
        self.assertLess(estimate_tokens(json.dumps(small)), estimate_tokens(json.dumps(full)) * .85)
        for a, b in zip(full, small):
            self.assertEqual(a["function"]["name"], b["function"]["name"])
            pa, pb = a["function"].get("parameters") or {}, b["function"].get("parameters") or {}
            self.assertEqual(pa.get("required"), pb.get("required"))
            self.assertEqual(set((pa.get("properties") or {})), set((pb.get("properties") or {})))
        weird = [{"type": "function", "function": {"name": "t", "description": "One. Two.", "parameters": {
            "type": "object", "properties": {"description": {"type": "string", "description": "Kept name. Extra."}}}}}]
        out = tools.compact_schemas(weird)[0]["function"]
        self.assertEqual(out["description"], "One.")
        self.assertIn("description", out["parameters"]["properties"])
        self.assertEqual(out["parameters"]["properties"]["description"]["description"], "Kept name.")


if __name__ == "__main__":
    unittest.main()
