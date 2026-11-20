"""Round 6: SnapWork steps and progress, scheduled tasks, the inside view, the theme setting."""
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from helpers import make_app, onboard, wait_until

from snaplab import plan as planmod
from snaplab import tasks as tasksmod


class PlanTest(unittest.TestCase):
    def test_checklist_is_read(self):
        pl = planmod.parse("# Plan\n- [x] Read BRIEF.md\n- [X] Write tests\n* [ ] Add subtract\n1. [ ] Run the tests\nnot a step")
        self.assertEqual((pl["done"], pl["total"], pl["now"]), (2, 4, "Add subtract"))
        self.assertIsNone(planmod.parse("no steps here"))
        self.assertEqual(planmod.total([pl, None, {"done": 1, "total": 2}]), {"done": 3, "total": 6, "works": 2})

    def test_bot_harness_writes_its_plan_and_progress_reaches_the_project(self):
        steps = [{"text": "Look up sources", "done": True}, {"text": "Write notes.md", "done": False}]

        def responder(messages, tools, effort):
            names = [t["function"]["name"] for t in tools or []]
            if "update_plan" in names and not any(m.get("role") == "tool" for m in messages):
                return {"content": "", "tool_calls": [{"name": "update_plan", "arguments": {"steps": steps}}]}
            return {"content": "Done: wrote the notes."}
        app, _, _ = make_app(responder)
        onboard(app)
        p = app.create_project(name="Notes")
        s = app.sessions.create(p, "bot", "Collect notes", started_by="user")
        self.assertTrue(wait_until(lambda: app.store.session(s["id"])["status"] in ("review", "failed"), 10))
        s = app.store.session(s["id"])
        self.assertEqual((s["plan"]["done"], s["plan"]["total"]), (1, 2))
        view = next(x for x in app.projects_view() if x["id"] == p["id"])
        self.assertEqual(view["progress"]["total"], 2)
        summ = app.sessions.summary(s)
        self.assertEqual(summ["plan"]["now"], "Write notes.md")
        from snaplab import prompts
        block = prompts.state_block(bot=app.store.orchestrator(), projects=app.store.projects(), sessions=app.store.sessions(),
                                    names={p["id"]: p["name"]})
        self.assertIn("steps 1/2", block)

    def test_codex_is_asked_to_keep_a_plan(self):
        from snaplab.sessions import brief_prompt
        text = brief_prompt("Add subtract", {"test_command": "python -m unittest"})
        self.assertIn(".snaplab/PLAN.md", text)


class TaskTest(unittest.TestCase):
    def test_schedules(self):
        now = dt.datetime(2026, 11, 2, 7, 0).astimezone()
        daily = tasksmod.clean_schedule({"kind": "daily", "time": "8:00"})
        self.assertEqual(daily, {"kind": "daily", "time": "08:00"})
        self.assertEqual(tasksmod.next_run(daily, now).hour, 8)
        self.assertEqual(tasksmod.next_run(daily, now.replace(hour=9)).day, 3)
        weekly = tasksmod.clean_schedule({"kind": "weekly", "time": "08:00", "days": ["thu", "Mon"]})
        self.assertEqual(weekly["days"], ["mon", "thu"])
        self.assertEqual(tasksmod.next_run(weekly, now.replace(hour=9)).strftime("%a"), "Thu")    # 2026-11-02 is a Monday
        self.assertEqual(tasksmod.clean_schedule({"kind": "every", "every_minutes": 10})["every_minutes"], 10)
        with self.assertRaises(ValueError):
            tasksmod.clean_schedule({"kind": "every", "every_minutes": 0})            # at least 1 minute
        with self.assertRaises(ValueError):
            tasksmod.clean_schedule({"kind": "daily", "time": "25:00"})

    def test_a_bot_asks_first_then_the_task_runs_in_its_chat(self):
        def responder(messages, tools, effort):
            last = messages[-1]["content"]
            if "Scheduled task" in last:
                return {"content": "Checked: nothing new today."}
            if "every morning" in last and not any(m.get("role") == "tool" for m in messages):
                return {"content": "", "tool_calls": [{"name": "schedule_task", "arguments": {
                    "title": "Morning news", "prompt": "Check the Python news", "kind": "daily", "time": "08:00"}}]}
            return {"content": "I asked you on a card."}
        app, llm, _ = make_app(responder)
        onboard(app)
        lead = app.store.orchestrator()
        room = app.store.dm_room(lead["id"])["id"]
        app.chat.post(room, "every morning at 8, check the Python news")
        self.assertTrue(wait_until(lambda: app.store.questions()))
        q = app.store.questions()[-1]
        self.assertEqual(q["action"]["type"], "schedule_task")
        self.assertIn(["prop.f.when", "@task.when.daily|time=08:00"], q["prop"]["fields"])
        self.assertEqual(app.tasks.all(), [])
        app.chat.answer(q["id"], "Approve")
        self.assertTrue(wait_until(lambda: app.tasks.all()))
        tk = app.tasks.all()[0]
        self.assertEqual((tk["bot"], tk["schedule"]["time"]), (lead["id"], "08:00"))
        # it is due: the run goes into the bot's chat, behind chats in the slot order
        due = dt.datetime.fromisoformat(tk["next_run"]) + dt.timedelta(minutes=1)
        self.assertEqual(app.tasks.tick(due), [tk["id"]])
        self.assertTrue(wait_until(lambda: any("nothing new" in (m.get("text") or "") for m in app.store.messages(room))))
        self.assertIn("session", [c["kind"] for c in llm.calls if "Scheduled task" in c["messages"][-1]["content"]])
        self.assertEqual(app.tasks.tick(due), [])                       # not twice
        # long missed runs are skipped, not piled up
        app.tasks.change(tk["id"], "pause")
        self.assertEqual(app.tasks.tick(due + dt.timedelta(days=2)), [])
        app.tasks.change(tk["id"], "resume")
        late = dt.datetime.fromisoformat(app.tasks.get(tk["id"])["next_run"]) + dt.timedelta(hours=9)
        self.assertEqual(app.tasks.tick(late), [])
        self.assertEqual(app.tasks.get(tk["id"])["missed"], 1)
        app.tasks.change(tk["id"], "delete")
        self.assertEqual(app.tasks.all(), [])

    def test_teammates_change_only_their_own_tasks(self):
        from snaplab.tools import Toolbox
        app, _, _ = make_app()
        onboard(app)
        lead = app.store.orchestrator()
        ken = app.create_bot(name="Ken")
        tk = app.tasks.create(bot_id=lead["id"], title="Lead task", prompt="x", schedule={"kind": "every", "every_minutes": 60})
        box = Toolbox(app, app.store.bot(ken["id"]), room=app.store.dm_room(ken["id"]), lang="en")
        self.assertIn("another bot", box("stop_task", {"task_id": tk["id"], "action": "delete"}, "c1").content)
        self.assertEqual(box("list_tasks", {}, "c2").content, "No scheduled tasks.")


class InsideTest(unittest.TestCase):
    def test_inside_view_and_theme(self):
        app, _, _ = make_app()
        onboard(app)
        p = app.create_project(name="Calc")
        ken = app.create_bot(name="Ken", projects=[p["id"]])
        v = app.inside_view(ken["id"])
        self.assertEqual([x["id"] for x in v["projects"]], [p["id"]])
        self.assertIn("persona", [x["name"] for x in v["view"]["sections"]])
        self.assertTrue(v["view"]["report"]["total"] > 0)
        lead = app.inside_view(app.store.orchestrator()["id"])
        self.assertEqual([x["id"] for x in lead["projects"]], [p["id"]])
        self.assertEqual(app.store.settings()["theme"], "system")


if __name__ == "__main__":
    unittest.main()
