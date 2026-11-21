"""What a bot sees: one place per rule, no contradictions, its own name apart from its model, and tool records
that cannot be mistaken for the bot's own words. Synthetic data only."""
import re
import unittest

from helpers import make_app, onboard
from snaplab import prompts
from snaplab.app import brief_text
from snaplab.tools import Toolbox


class PromptCleanupTest(unittest.TestCase):
    def setUp(self):
        self.app, _, _ = make_app(lambda m, t, e: {"content": "ok"},
                                  codex={"backend": "ssh", "remote": {"host": "agent.invalid"}})
        self.addCleanup(self.app.archive.close)
        info = onboard(self.app)
        self.room, self.bot = info["room"], self.app.store.bot(info["bot"]["id"])

    def system(self):
        return self.app.chat.context.build(self.room, self.bot, "en")[0][0]["content"]

    def test_tool_records_are_snaplab_lines_after_the_reply(self):
        st = self.app.store
        st.add_message(self.room["id"], {"role": "user", "text": "Start the work"})
        st.add_message(self.room["id"], {"role": "bot", "bot": self.bot["id"], "text": "Started.", "status": "done",
                                         "tools": [{"name": "start_session", "label": "Game"}]})
        st.add_message(self.room["id"], {"role": "user", "text": "How is it going?"})
        messages, _ = self.app.chat.context.build(self.room, self.bot, "en")
        mine = [m["content"] for m in messages if m["role"] == "assistant"]
        self.assertTrue(mine)
        self.assertFalse(any("tools you used" in c or "[SnapLab record]" in c or "start_session" in c for c in mine))
        last = messages[-1]["content"]
        self.assertIn("[SnapLab record] Your previous turn: you called start_session 'Game'.", last)
        self.assertLess(last.index("[SnapLab record]"), last.index("How is it going?"))

    def test_a_record_as_the_last_entry_still_asks_to_continue(self):
        self.app.store.add_message(self.room["id"], {"role": "bot", "bot": self.bot["id"], "text": "Done.",
                                                     "status": "done", "tools": [{"name": "write_memory"}]})
        messages, _ = self.app.chat.context.build(self.room, self.bot, "en")
        self.assertEqual(messages[-1]["role"], "user")
        self.assertTrue(messages[-1]["content"].rstrip().endswith("[SnapLab note] Continue."))

    def test_bot_name_and_model_are_separate_facts(self):
        text = self.system()
        self.assertIn('You are the bot "Marlin". The user chose this name; it is not the name of a model.', text)
        self.assertIn('model id "qwen3.8-27b"', text)

    def test_harness_choice_is_explained_and_not_hidden(self):
        text = self.system()
        self.assertIn("When you omit harness".lower(), text.lower())
        self.assertIn("all other work goes to Pi", text)
        self.assertNotIn("name Pi/Codex", text)
        self.assertNotIn("internal details", text)
        local = prompts.house_rules("en", {"codex": {"backend": "local"}})
        self.assertIn("SnapLab uses Codex", local)

    def test_one_place_for_screenshots_files_and_desktop_operations(self):
        text = self.system()
        # No commands for SnapCode in the bot's prompt: its own instructions have them (python vs python3 differed).
        self.assertNotRegex(text, r"python3? \.snaplab/")
        self.assertEqual(text.count("attachments.json"), 1)
        self.assertNotIn("CAPTCHA", text)                 # only the payment stop is kept (2026-11-21)
        self.assertIn("SnapCode stops at a payment", text)
        self.assertIn("clicks by x,y, by selector, by visible text or by number on a marked screenshot", text)
        self.assertEqual(prompts.SNAPCODE_ABILITIES.count("by x,y"), 1)
        self.assertIn("DevTools", text)
        # HTML: the phone offers it as a file to save (attachments.response serves it as a download only).
        self.assertIn("HTML arrives as a file to save and does not run in the chat", text)
        tools = {t["function"]["name"]: t["function"]["description"]
                 for t in Toolbox(self.app, self.bot, room=self.room).schemas()}
        self.assertNotIn("opens as an isolated", tools["publish_file"])
        self.assertIn("does not run in the chat", tools["publish_file"])
        self.assertNotRegex(tools["publish_file"], r"python3? \.snaplab/")

    def test_house_rules_carry_no_tool_record_wording(self):
        self.assertNotIn("(tools you used", self.system())

    def test_brief_says_who_checks_and_drops_standard_library_on_linux(self):
        for lang in ("en", "ja", "ru"):
            ssh, local = brief_text("p", "goal", "", lang, ssh=True), brief_text("p", "goal", "", lang)
            self.assertNotIn("あなたが結果を確かめる", ssh)
            self.assertIn("SnapCode", ssh)
            self.assertEqual(len(re.findall(r"^- ", local, re.M)) - len(re.findall(r"^- ", ssh, re.M)), 1)
        self.assertNotIn("standard library", brief_text("p", "goal", "", "en", ssh=True))
        self.assertIn("standard library", brief_text("p", "goal", "", "en"))


if __name__ == "__main__":
    unittest.main()
