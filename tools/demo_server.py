"""Run SnapLab with a scripted model (no API calls) for UI work and screenshots.

    python tools/demo_server.py --data <folder> --port 18446 [--seed]

The model is a stand-in that streams thinking and text with small delays and calls tools on cue words
("search", "dashboard", "project", "ask"). "fail" makes the first try of that message fail with HTTP 503
(Try again then works) and "cut" ends the stream halfway, to show the error states. Requests must carry `Tailscale-User-Login: demo@example.com`
(the e2e driver sets it); pair with the link this script prints.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from snaplab import config, server  # noqa: E402
from snaplab.app import App  # noqa: E402
from snaplab.llm import Reply  # noqa: E402


app_ref: dict = {}


class DemoLLM:
    is_real = False

    def __init__(self, delay: float = 0.03):
        self.delay = delay
        self.failed: set[str] = set()

    def has_key(self):
        return True

    def key(self):
        return None

    def stream_chat(self, messages, *, tools=None, effort="low", max_tokens=4096, on_reasoning=None, on_content=None,
                    cancel=None, source="chat", kind="chat", on_wait=None):
        last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        if "[end of SnapLab context]" in str(last_user):        # the context block is not part of what the user said
            last_user = str(last_user).split("[end of SnapLab context]", 1)[1].strip()
        last = messages[-1]
        ja = any("぀" <= ch <= "ヿ" for ch in str(last_user))
        if "r10-" in str(last_user):
            ja = app_ref["app"].lang == "ja"
        think = ("The user wants something. Let me decide which tool helps, keep it short, and answer clearly. "
                 "I should not run code myself; sessions run sandboxed.") * 2
        calls = []
        text = ""
        if last["role"] == "tool":
            name = next((m for m in reversed(messages) if m.get("tool_calls")), {}).get("tool_calls", [{}])[0].get("function", {}).get("name")
            text = {"web_search": ("検索結果を見ました。要点は次のとおりです：\n- 公式ドキュメントが一番まとまっています\n- 最新版は **3.13**\n\n詳しくはリンクを見てください。"
                                   if ja else "Here's what I found:\n- The official docs are the best summary\n- The latest release is **3.13**\n\nWant me to read one of the pages?"),
                    "update_dashboard": "ダッシュボードを更新しました。" if ja else "Done — the dashboard now shows today's plan.",
                    "create_project": "プロジェクトを作りました。" if ja else "Project created. Shall I start a Codex session for it?",
                    "write_file": ("notes/summary.md に3行でまとめました。" if ja else "Wrote a three-line summary to notes/summary.md."),
                    "recall": ("見つかりました。10月30日に「テストは unittest で」と決めています（発言の記録より）。" if ja else
                               "Found it: on October 30 we decided to use unittest for the tests (from the chat record)."),
                    }.get(name, "OK." if not ja else "わかりました。")
            if "r10-web-approval" in str(last_user) and name == "web_search":
                text = ""
                calls = [{"name": "create_project", "arguments": {"name": "Round10 Web Notes",
                           "description": "ウェブの資料をまとめます。" if app_ref["app"].lang == "ja" else "Collect web notes."}}]
        elif "[Result card]" in str(last_user):
            text = ("終わりました。notes/summary.md に3行のメモができています（ファイル1つ）。main に取り込みますか？" if ja else
                    "Done: the note is in notes/summary.md (1 file, 3 lines). Shall I merge it into main?")
        else:
            low = str(last_user).lower()
            if "[Reference]" in str(last_user) and any(x in str(last_user) for x in ("Round10 request:", "Round10 reply:")):
                ja = app_ref["app"].lang == "ja"
                text = "[SILENT]" if "Round10 reply:" in str(last_user) else (
                    "Round10 reply: 縦に時間を並べ、予定を色で区別する案を確認しました。" if ja else
                    "Round10 reply: I checked the vertical timeline and the colours for each event.")
            elif tools and "r10-contact:" in low:
                target = re.search(r"r10-contact:(b-[0-9a-f]+)", low).group(1)
                calls = [{"name": "message_bot", "arguments": {"bot": target,
                    "text": "Round10 request: 時間を縦に並べる予定表の案を確認してください。" if app_ref["app"].lang == "ja" else
                            "Round10 request: Please review the vertical schedule layout."}}]
            elif tools and "r10-group:" in low:
                targets = re.search(r"r10-group:([b0-9a-f,\-]+)", low).group(1).split(",")
                calls = [{"name": "create_group", "arguments": {"name": "Round10 planning", "bots": targets,
                    "text": "Round10 request: 予定表を一緒に検討してください。" if app_ref["app"].lang == "ja" else
                            "Round10 request: Let's discuss the schedule together."}}]
            elif tools and "r10-project-" in low:
                number = re.search(r"r10-project-(\d+)", low).group(1)
                calls = [{"name": "create_project", "arguments": {"name": "Round10 Calendar " + number,
                    "description": "時間を縦に並べる予定表です。" if app_ref["app"].lang == "ja" else "A vertical calendar."}}]
            elif tools and "r10-web-approval" in low:
                calls = [{"name": "web_search", "arguments": {"query": "calendar layout"}}]
            elif tools and ("search" in low or "調べ" in low):
                calls = [{"name": "web_search", "arguments": {"query": "python latest release"}}]
            elif tools and ("dashboard" in low or "ダッシュボード" in low):
                calls = [{"name": "update_dashboard", "arguments": {"op": "upsert", "widgets": [
                    {"id": "plan", "type": "list", "title": "Today" if not ja else "今日の予定", "size": "full", "items": [
                        {"text": "Review the session results" if not ja else "セッションの結果を見る", "meta": "10:00", "tone": "accent"},
                        {"text": "Ship v0.1 to Richard" if not ja else "v0.1 を Richard さんに見せる", "meta": "15:00", "tone": "good"},
                        {"text": "Plan v0.2" if not ja else "v0.2 の計画", "tone": "neutral"}]},
                    {"id": "focus", "type": "stat", "title": "Focus" if not ja else "集中", "value": "82", "unit": "%", "note": "+6 vs yesterday" if not ja else "昨日より +6", "tone": "good"},
                    {"id": "ship", "type": "progress", "title": "v0.1 progress" if not ja else "v0.1 の進み具合", "value": 70, "tone": "accent"}]}}]
            elif tools and ("every morning" in low or "毎朝" in low):
                calls = [{"name": "schedule_task", "arguments": {"title": "朝のニュース" if ja else "Morning news",
                                                                 "prompt": "Python の新しいニュースを確かめて3行で知らせる" if ja else "Check new Python news and tell me in three lines",
                                                                 "kind": "daily", "time": "08:00"}}]
            elif tools and ("remember when" in low or "what did we decide" in low or "前に" in low or "決めた" in low):
                calls = [{"name": "recall", "arguments": {"query": "unittest テスト" if ja else "unittest tests"}}]
            elif tools and ("ask" in low or "聞いて" in low):
                calls = [{"name": "ask_user", "arguments": {"question": "Which test framework do you prefer?" if not ja else "テストの形式はどちらがいいですか？",
                                                            "options": ["unittest", "pytest"]}}]
            elif tools and any(t["function"]["name"] == "write_file" for t in tools):
                calls = [{"name": "write_file", "arguments": {"path": "notes/summary.md", "content":
                          "# Summary\n\n- Local models are popular for privacy\n- People want sessions they can watch\n- Short answers win on phones\n"}}]
            elif tools and ("session" in low or "セッション" in low):
                p = (app_ref["app"].store.projects() or [{}])[-1]
                calls = [{"name": "start_session", "arguments": {"project": p.get("id", ""), "harness": "bot",
                                                                 "prompt": "メモを notes/summary.md にまとめて" if ja else "Summarise the findings into notes/summary.md",
                                                                 "title": "要点のメモ" if ja else "Summary note"}}]
            elif tools and ("project" in low or "プロジェクト" in low):
                calls = [{"name": "create_project", "arguments": {"name": f"Demo tool {int(time.time()) % 1000}", "description": "A tiny CLI made by Codex"}}]
            else:
                text = ("了解です！**SnapLab** の準備はできています。\n\n- 調べもの\n- プロジェクトの用意\n- セッションの見守り\n\nなんでも頼んでください。" if ja else
                        "Sure thing! **SnapLab** is ready.\n\nI can:\n- research things on the web\n- set up projects\n- start and watch coding sessions\n\nWhat should we do first?")
        low_user = str(last_user).lower()
        if last["role"] != "tool" and "fail" in low_user and last_user not in self.failed:
            self.failed.add(last_user)
            from snaplab.llm import ApiError
            time.sleep(0.6)
            raise ApiError(503, 'HTTP 503: {"error": {"message": "upstream busy", "type": "server_error"}}',
                           "server-side problem (shared GPU busy or down)")
        if last["role"] != "tool" and "cut" in low_user and not calls:
            text = ("Here is the start of a longer answer about sandboxes: the session folder is the only place"
                    if not ja else "囲いについての長い答えの始まりです。作業場所だけが")
            for i in range(0, len(text), 3):
                if on_content:
                    on_content(text[i:i + 3])
                time.sleep(self.delay)
            from snaplab.llm import StreamCut
            raise StreamCut(Reply(content=text, reasoning="", finished=False))
        for i in range(0, len(think), 12):
            if cancel and cancel():
                from snaplab.llm import Cancelled
                raise Cancelled()
            if on_reasoning:
                on_reasoning(think[i:i + 12])
            time.sleep(self.delay / 2)
        for i in range(0, len(text), 3):
            if on_content:
                on_content(text[i:i + 3])
            time.sleep(self.delay)
        tc = [{"id": f"call_{i}", "name": c["name"], "arguments": json.dumps(c["arguments"], ensure_ascii=False)} for i, c in enumerate(calls)]
        return Reply(content=text, reasoning=think, tool_calls=tc, finish_reason="tool_calls" if tc else "stop",
                     input_tokens=1200, output_tokens=180)


class DemoSearch:
    def search(self, query, count=5):
        return {"provider": "demo", "query": query, "results": [
            {"title": "Python 3.13 documentation", "url": "https://docs.python.org/3/", "snippet": "The official documentation."}]}


def seed(app, lang: str) -> None:
    """Sample bots, projects and sessions in every state, so each screen has something to show."""
    import datetime as dt
    from snaplab.store import new_id
    ja = lang == "ja"
    st = app.store
    st.update_settings(lang=lang)
    coral = app.create_bot(name="Coral", role="調べもの担当。出典つきで短くまとめる" if ja else "Researcher: finds sources and sums them up", color="#12A37F")
    nemo = app.create_bot(name="Nemo", role="試験担当。壊れやすいところを探す" if ja else "Tester: hunts for the weak spots", color="#8B5CF6")
    p1 = app.create_project(name="ミニ電卓" if ja else "Tiny calculator", description=("Codex が作る小さな電卓 CLI。足し算から。" if ja else "A tiny calculator CLI built by Codex, starting with addition."),
                            test_command="python -m unittest -v", bot=nemo["id"])
    p2 = app.create_project(name="市場調査メモ" if ja else "Market notes", description=("ローカル LLM のアプリの動向を調べてまとめる。" if ja else "Research notes on local-LLM apps and what users ask for."), bot=coral["id"])
    p3 = app.create_project(name="ToDo アプリ" if ja else "Todo app", description=("JSON に保存する ToDo の CLI。" if ja else "A todo CLI that saves to JSON."), test_command="python -m unittest -v")
    now = dt.datetime.now().astimezone()

    def ts(minutes):
        return (now - dt.timedelta(minutes=minutes)).isoformat(timespec="seconds")

    def plan(*steps, done=0):
        from snaplab import plan as planmod
        return planmod.parse("\n".join(f"- [{'x' if i < done else ' '}] {x}" for i, x in enumerate(steps)))

    def mk(project, harness, title, status, minutes, elapsed, tin, tout, files, tests=None, last="", error=None, events=(), pl=None):
        sid = new_id("s")
        s = {"id": sid, "project": project["id"], "harness": harness, "title": title, "status": status, "created": ts(minutes + 2),
             "started": ts(minutes if status != "running" else 0.6), "ended": ts(minutes - 1) if status != "running" else None,
             "started_by": "user", "room": None, "bot": coral["id"] if harness == "bot" else None,
             "elapsed_s": elapsed if status != "running" else 0, "tokens": {"input": tin, "output": tout}, "calls": 6, "turns": 1,
             "changed_files": files, "tests": tests, "last_prompt": title, "last_message": last, "error": error, "thread_id": None,
             "plan": pl}
        st.save_session(s)
        for e in events:
            st.session_event(sid, dict(e))
        return s
    ev_codex = [{"kind": "prompt", "text": "Create calc.py with add(a, b) and sub(a, b), plus unittest tests."},
                {"kind": "message", "text": "I'll create the module and the tests, then run them."},
                {"kind": "command", "text": "Get-ChildItem", "exit_code": 0},
                {"kind": "file", "text": "add calc.py, add test_calc.py"},
                {"kind": "command", "text": "python -m unittest -v", "exit_code": 0, "output": "Ran 4 tests in 0.002s\n\nOK"},
                {"kind": "message", "text": "Done. calc.py has add/sub; 4 tests pass."},
                {"kind": "tests", "text": "Tests: passed", "passed": 4, "failed": 0},
                {"kind": "files", "text": "2 files changed", "files": ["added: calc.py", "added: test_calc.py"]}]
    mk(p1, "codex", "足し算と引き算を作って、試験も書いて" if ja else "Add and subtract, with tests", "review", 42, 96.4, 61234, 2210,
       [{"path": "calc.py", "change": "added"}, {"path": "test_calc.py", "change": "added"}],
       {"status": "passed", "exit_code": 0, "passed": 4, "failed": 0, "output": "Ran 4 tests in 0.002s\n\nOK", "command": "python -m unittest -v"},
       last=("できました。calc.py に add と sub、試験 4 件はすべて成功です。" if ja else "Done. `calc.py` has `add` and `sub`; all **4 tests pass**."), events=ev_codex,
       pl=plan(*(["BRIEF.md を読む", "add と sub を書く", "試験を書く", "試験を動かす"] if ja else ["Read BRIEF.md", "Write add and sub", "Write the tests", "Run the tests"]), done=4))
    mk(p1, "codex", "割り算を足して、0 で割ったときの扱いも" if ja else "Add division and handle divide-by-zero", "running", 1, 0, 18450, 640,
       [{"path": "calc.py", "change": "modified"}], None, events=ev_codex[:4],
       pl=plan(*(["今のコードを読む", "割り算を足す", "0 で割ったときのエラー", "試験を足す", "試験を動かす"] if ja else
                 ["Read the current code", "Add divide", "Error for divide-by-zero", "Add tests", "Run the tests"]), done=2))
    mk(p2, "bot", "ローカル LLM アプリの人気の機能を5つ" if ja else "Top 5 features people want in local-LLM apps", "waiting", 15, 54.0, 22800, 1900,
       [{"path": "notes/features.md", "change": "added"}], None,
       last=("5つまとめました（notes/features.md）。次は、価格の比較も調べますか？" if ja else "I wrote five of them to notes/features.md. Should I also compare pricing?"),
       events=[{"kind": "prompt", "text": "Top 5 features"}, {"kind": "tool", "text": "web_search local LLM app features", "status": "ok"},
               {"kind": "tool", "text": "write_file notes/features.md", "status": "ok"}, {"kind": "message", "text": "I wrote five of them."}],
       pl=plan(*(["出典を探す", "機能を5つ選ぶ", "notes/features.md に書く"] if ja else ["Find sources", "Pick five features", "Write notes/features.md"]), done=3))
    mk(p3, "codex", "保存の形式を決めて実装" if ja else "Pick a storage format and implement it", "failed", 180, 212.0, 88200, 4100,
       [{"path": "todo.py", "change": "added"}],
       {"status": "failed", "exit_code": 1, "passed": 2, "failed": 1, "output": "Ran 3 tests\nFAILED (failures=1)", "command": "python -m unittest -v"},
       last="", error=("試験が 1 件失敗しました（test_save_load）" if ja else "1 test failed (test_save_load)"),
       events=[{"kind": "prompt", "text": "Implement storage"}, {"kind": "command", "text": "python -m unittest -v", "exit_code": 1, "output": "FAILED (failures=1)"},
               {"kind": "error", "text": "1 test failed"}],
       pl=plan(*(["形式を決める", "保存を書く", "読み込みを書く", "試験を通す"] if ja else ["Choose a format", "Write save", "Write load", "Make the tests pass"]), done=2))
    # scheduled tasks and a little history in the chats, so the inside screens and the record have something to show
    app.tasks.create(bot_id=coral["id"], title="朝のニュース" if ja else "Morning news",
                     prompt="ローカル LLM のニュースを3行で" if ja else "Local-LLM news in three lines", schedule={"kind": "daily", "time": "08:00"})
    app.tasks.create(bot_id=nemo["id"], title="試験の見回り" if ja else "Test check", prompt="試験を動かして、落ちたら知らせる" if ja else "Run the tests and tell me if any fail",
                     schedule={"kind": "weekly", "time": "18:00", "days": ["mon", "thu"]})
    for b_, lines in ((nemo, (("テストは unittest でいい？", "はい、unittest にしましょう。依存が増えません。") if ja else
                              ("Shall we use unittest for the tests?", "Yes, unittest: no extra dependency."))),
                      (coral, (("価格の比較も調べておいて", "了解です。3つのアプリの価格を表にします。") if ja else
                               ("Also compare the pricing", "Sure, I'll put three apps' prices in a table.")))):
        room = st.dm_room(b_["id"])
        st.add_message(room["id"], {"role": "user", "text": lines[0], "ts": ts(60 * 24)})
        st.add_message(room["id"], {"role": "bot", "bot": b_["id"], "status": "done", "text": lines[1], "ts": ts(60 * 24 - 1)})
    app.memory_write(nemo["id"], "unittest を使う（依存を増やさない）" if ja else "Use unittest (no extra dependency)")
    u = st.read_json("usage.json", {})
    days = {}
    for i, n in enumerate([12, 30, 18, 44, 26, 51, 37]):
        d = (dt.date.today() - dt.timedelta(days=6 - i)).isoformat()
        days[d] = {"calls": n, "input_tokens": n * 2900, "output_tokens": n * 160}
    u["days"] = days
    u["total"] = {"calls": sum(v["calls"] for v in days.values())}
    st.write_json("usage.json", u)
    for text, kind in ((("Nemo が試験を足しました" if ja else "Nemo added tests to Tiny calculator"), "bot"),
                       (("セッション「保存の形式」が失敗しました" if ja else "Session \"Pick a storage format\" failed"), "error")):
        st.activity(kind, text)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--data", required=True)
    p.add_argument("--port", type=int, default=18446)
    p.add_argument("--proxy-port", type=int, default=18433)
    p.add_argument("--delay", type=float, default=0.03)
    p.add_argument("--seed", choices=["en", "ja"], default=None, help="add sample bots, projects and sessions")
    p.add_argument("--two-models", action="store_true", help="also list a second (local) model, to show the model menus")
    a = p.parse_args()
    over = {"data_dir": a.data, "listen_port": a.port, "owner_login": "demo@example.com",
            "projects_dir": str(Path(a.data).resolve() / "projects"),
            "slots": {"dir": str(Path(a.data).resolve() / "locks")},
            "proxy": {"port": a.proxy_port}, "public_url": f"http://127.0.0.1:{a.port}", "api": {"key_expires": "2026-11-29"}}
    if a.two_models:
        over["providers"] = {"local": {"base_url": "http://127.0.0.1:11434/v1", "api_key_env": "LOCAL_LLM_KEY", "slots": {"count": 1}}}
        over["models"] = {"local": {"provider": "local", "model": "llama-local", "context_limit": 32768,
                                    "effort_style": "none", "label": "Local model (example)"}}
    # A screenshot demo must never inherit the live config, data, project or lock directories.
    for name in ("SNAPLAB_DATA", "SNAPLAB_CONFIG", "SNAPLAB_PORT"):
        os.environ.pop(name, None)
    with tempfile.TemporaryDirectory(prefix="snaplab-demo-config-") as config_root:
        c = config.load(path=Path(config_root) / "none.json", overrides=over)
    app = App(c, llm=DemoLLM(a.delay), searcher=DemoSearch(), owner="demo@example.com", start_translator=False)
    app_ref["app"] = app
    if a.seed and not app.store.projects():
        seed(app, a.seed)
    token, _ = app.auth.new_pairing("demo")
    print(f"PAIR http://127.0.0.1:{a.port}/pair/{token}", flush=True)
    server.serve_forever(app, "127.0.0.1", a.port)


if __name__ == "__main__":
    main()
