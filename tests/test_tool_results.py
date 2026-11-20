"""Tool results are sized to the model's context window, not to a fixed number of characters."""
import re
import unittest
from types import SimpleNamespace

from helpers import make_app, onboard

from snaplab.agent import ToolResult, fit_tool_result, run_loop
from snaplab.llm import Reply

POSTS = "".join(f"### @user{i}\n> post number {i} about models and chips\n- ID: {10**18 + i}\n\n" for i in range(1400))


class Model:
    """Answers with one tool call, then a final text; records what it was sent."""

    def __init__(self, window):
        self.api_cfg = {"context_limit": window}
        self.seen = []

    def stream_chat(self, messages, tools=None, **kw):
        self.seen.append([dict(m) for m in messages])
        if len(self.seen) == 1:
            return Reply(tool_calls=[{"id": "c1", "name": "xw_get_new_posts", "arguments": "{}"}])
        return Reply(content="done")


class ToolResultSizeTest(unittest.TestCase):
    def test_big_result_passes_whole_when_the_window_has_room(self):
        self.assertGreater(len(POSTS), 60000)
        llm = Model(131072)
        run_loop(llm, [{"role": "user", "content": "news"}], [], lambda *a: ToolResult(POSTS),
                 effort="low", max_steps=4, max_tokens=4096, source="session")
        tool_msg = [m for m in llm.seen[1] if m["role"] == "tool"][0]
        self.assertEqual(tool_msg["content"], POSTS)

    def test_what_does_not_fit_points_to_the_archived_rest(self):
        app, _, _ = make_app()
        bot = onboard(app)["bot"]
        session = {"id": "s-0000000d", "bot": bot["id"], "project": "p-1", "harness": "bot", "status": "running"}
        from snaplab.tools import Toolbox
        box = Toolbox(app, bot, session=session)
        res = ToolResult(POSTS)
        box._archive_call("xw_get_new_posts", {}, res, {"status": "ok", "label": ""})
        self.assertTrue(res.record)
        shown = fit_tool_result(res, [{"role": "user", "content": "news"}], [], SimpleNamespace(api_cfg={"context_limit": 16000}), 2048)
        keep = len(shown.split("\n\n[SnapLab:")[0])
        self.assertTrue(POSTS.startswith(shown[:keep]))
        self.assertGreater(keep, 10000)
        m = re.search(r'recall\(record="([^"]+)", from_char=(\d+)\)', shown)
        self.assertEqual(m.group(1), res.record)
        rest = box.t_recall(record=res.record, from_char=int(m.group(2)), max_chars=200000).content
        self.assertIn(POSTS[keep:keep + 500], rest)
        self.assertIn(POSTS[-200:], rest)                 # a large max_chars reads the remainder in one call

    def test_unknown_window_keeps_the_old_clip(self):
        shown = fit_tool_result(ToolResult(POSTS), [], [], SimpleNamespace(), 4096)
        self.assertLess(len(shown), 15000)


if __name__ == "__main__":
    unittest.main()
