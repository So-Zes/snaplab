"""Config, slots, privacy filter, dashboard validation, auth."""
import datetime as dt
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from helpers import ROOT

from snaplab import auth, config
from snaplab import dashboard as dash
from snaplab.privacy import Redactor
from snaplab.slots import Slots, SlotsBusy


class ConfigTest(unittest.TestCase):
    def test_defaults_and_overrides(self):
        c = config.load(path=Path(tempfile.mkdtemp()) / "missing.json", overrides={"api": {"chat_effort": "low"}})
        self.assertEqual(c["api"]["model"], "configure-model")
        self.assertEqual(c["slots"]["count"], 2)
        self.assertEqual(c["listen_host"], "127.0.0.1")
        self.assertEqual(c["tailscale_https_port"], 8445)
        self.assertTrue(c["projects_dir"].endswith("projects"))
        self.assertEqual(c["slots"]["count"], 2)

    def test_env_data_dir(self):
        d = tempfile.mkdtemp()
        os.environ["SNAPLAB_DATA"] = d
        try:
            c = config.load(path=Path(d) / "x.json")
            self.assertEqual(Path(c["data_dir"]), Path(d).resolve())
            self.assertEqual(Path(c["slots"]["dir"]), Path(d).resolve() / "locks")
        finally:
            del os.environ["SNAPLAB_DATA"]


class SlotsTest(unittest.TestCase):
    def test_limit_in_process(self):
        s = Slots(tempfile.mkdtemp(), 2)
        with s.acquire():
            with s.acquire():
                self.assertEqual(s.in_use(), 2)
                with self.assertRaises(SlotsBusy):
                    with s.acquire(wait=False):
                        pass
            self.assertEqual(s.in_use(), 1)
        self.assertEqual(s.in_use(), 0)

    def test_chat_goes_before_session_work(self):
        import threading
        s = Slots(tempfile.mkdtemp(), 1)
        order, started = [], []

        def worker(kind):
            with s.acquire(kind=kind, poll_s=0.02):
                order.append(kind)
                time.sleep(0.05)
        with s.acquire(kind="session"):
            t1 = threading.Thread(target=worker, args=("session",))
            t1.start()
            time.sleep(0.1)                      # the session waiter is first to queue ...
            t2 = threading.Thread(target=worker, args=("chat",))
            t2.start()
            time.sleep(0.1)
            self.assertEqual([w["kind"] for w in s.queue()], ["chat", "session"])   # ... but chat is ahead
        t1.join()
        t2.join()
        self.assertEqual(order, ["chat", "session"])

    def test_session_cap_leaves_room_for_chat(self):
        s = Slots(tempfile.mkdtemp(), 3, session_max=2)
        with s.acquire(kind="session"), s.acquire(kind="session"):
            with self.assertRaises(SlotsBusy):
                with s.acquire(kind="session", wait=False):
                    pass
            with s.acquire(kind="chat", wait=False):     # the third slot is still there for talking
                self.assertEqual(s.in_use(), 3)

    def test_shared_with_other_process_same_protocol(self):
        """Another process holding slot0.lock the research-folder way (snaplib) counts against our limit."""
        d = tempfile.mkdtemp()
        code = ("import msvcrt,time,sys\nf=open(sys.argv[1]+'/slot0.lock','a+');f.seek(0);msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)\n"
                "print('held',flush=True);time.sleep(3)") if os.name == "nt" else \
               ("import fcntl,time,sys\nf=open(sys.argv[1]+'/slot0.lock','a+');fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)\n"
                "print('held',flush=True);time.sleep(3)")
        p = subprocess.Popen([sys.executable, "-c", code, d], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(p.stdout.readline().strip(), "held")
            s = Slots(d, 1)
            self.assertEqual(s.in_use(), 1)
            with self.assertRaises(SlotsBusy):
                with s.acquire(wait=False):
                    pass
        finally:
            p.kill()
            p.wait()


class PrivacyTest(unittest.TestCase):
    def setUp(self):
        self.r = Redactor(extra_terms=["box-abc123.tailxyz.ts.net", "person@mail.example"], home=r"C:\Users\Kazuko",
                          username="Kazuko", computer="DESKTOP-XYZ123", secrets=["sk-secret-value"])

    def test_clean(self):
        t = (r"cwd C:\Users\Kazuko\proj and c:/users/kazuko/x, host box-abc123.tailxyz.ts.net, "
             "user Kazuko, pc DESKTOP-XYZ123, mail person@mail.example, key sk-secret-value")
        out = self.r.clean(t)
        for bad in ("Kazuko", "kazuko", "tailxyz", "DESKTOP-XYZ123", "person@mail", "sk-secret-value"):
            self.assertNotIn(bad, out)
        self.assertIn(r"C:\Users\snapuser\proj", out)
        self.assertTrue(self.r.contains_private("hello Kazuko"))
        self.assertFalse(self.r.contains_private("hello world"))

    def test_restore_paths_in_json(self):
        import json
        args = json.dumps({"path": r"C:\Users\snapuser\proj\a.py"})
        self.assertEqual(json.loads(self.r.restore_paths(args))["path"], r"C:\Users\Kazuko\proj\a.py")

    def test_common_words_not_redacted(self):
        r = Redactor(home=r"C:\Users\user", username="user", computer="pc")
        self.assertEqual(r.clean("the user said hi"), "the user said hi")

    def test_clean_obj(self):
        o = self.r.clean_obj({"messages": [{"content": "I am Kazuko"}], "n": 3})
        self.assertEqual(o["messages"][0]["content"], "I am snapuser")
        self.assertEqual(o["n"], 3)


class DashboardTest(unittest.TestCase):
    def test_valid_widgets(self):
        ws = dash.apply([], "set", [
            {"id": "a", "type": "stat", "title": "Tokens", "value": 1200, "unit": "k", "tone": "good"},
            {"id": "b", "type": "list", "items": ["one", {"text": "two", "meta": "10:00", "tone": "warn"}], "size": "full"},
            {"id": "c", "type": "progress", "value": 45.5},
            {"id": "d", "type": "table", "columns": ["x", "y"], "rows": [["1", "2"]]},
            {"id": "e", "type": "bars", "items": [{"label": "Mon", "value": 3}]},
            {"id": "f", "type": "note", "text": "<script>alert(1)</script> stays plain text"}])
        self.assertEqual([w["id"] for w in ws], list("abcdef"))
        self.assertEqual(ws[0]["value"], "1200")
        self.assertEqual(ws[1]["items"][0]["text"], "one")

    def test_rejects_bad_data(self):
        bad = [
            {"id": "a", "type": "html", "html": "<b>x</b>"},
            {"id": "a", "type": "stat", "value": 1, "onclick": "x"},
            {"id": "A B", "type": "stat", "value": 1},
            {"id": "a", "type": "progress", "value": 150},
            {"id": "a", "type": "note", "text": "x" * 601},
            {"id": "a", "type": "stat", "value": 1, "tone": "red"},
            {"id": "a", "type": "table", "columns": ["x"], "rows": [["1", "2"]]},
            {"id": "a", "type": "list", "items": [{"text": "x", "script": "y"}]},
        ]
        for w in bad:
            with self.assertRaises(dash.WidgetError, msg=str(w)):
                dash.apply([], "set", [w])

    def test_upsert_and_remove(self):
        cur = dash.apply([], "set", [{"id": "a", "type": "note", "text": "1"}, {"id": "b", "type": "note", "text": "2"}])
        cur = dash.apply(cur, "upsert", [{"id": "b", "type": "note", "text": "B"}, {"id": "c", "type": "note", "text": "3"}])
        self.assertEqual([(w["id"], w["text"]) for w in cur], [("a", "1"), ("b", "B"), ("c", "3")])
        cur = dash.apply(cur, "remove", ids=["a"])
        self.assertEqual([w["id"] for w in cur], ["b", "c"])
        with self.assertRaises(dash.WidgetError):
            dash.apply(cur, "set", [{"id": f"w{i}", "type": "note", "text": "x"} for i in range(17)])


class AuthTest(unittest.TestCase):
    def test_pairing_once_and_revoke(self):
        st = auth.AuthStore(Path(tempfile.mkdtemp()) / "auth.json")
        token, rec = st.new_pairing("phone")
        self.assertEqual(st.pairing_state(token)[0], "ok")
        state, value, dev = st.redeem(token, "Mozilla/5.0 (Linux; Android 15) Chrome/141")
        self.assertEqual(state, "ok")
        self.assertEqual(st.redeem(token)[0], "used")
        self.assertEqual(st.device_for(value)["id"], dev["id"])
        self.assertNotIn(token, (Path(st.path)).read_text())
        self.assertNotIn(value, (Path(st.path)).read_text())
        st.revoke(dev["id"])
        self.assertIsNone(st.device_for(value))

    def test_expired_and_revoked_links(self):
        st = auth.AuthStore(Path(tempfile.mkdtemp()) / "auth.json")
        token, rec = st.new_pairing("phone", ttl_hours=0.0001)
        time.sleep(0.5)
        self.assertEqual(st.redeem(token)[0], "expired")
        t2, r2 = st.new_pairing("phone2")
        st.revoke("all-links")
        self.assertEqual(st.redeem(t2)[0], "used")

    def test_check_user(self):
        ok, _ = auth.check_user({"Tailscale-User-Login": "A@x.com"}, "a@x.com")
        self.assertTrue(ok)
        self.assertFalse(auth.check_user({"Tailscale-User-Login": "b@x.com"}, "a@x.com")[0])
        self.assertFalse(auth.check_user({}, "a@x.com")[0])
        self.assertFalse(auth.check_user({"Tailscale-User-Login": "a@x.com"}, None)[0])
        self.assertFalse(auth.check_user({"Tailscale-User-Login": "a@x.com", "Tailscale-Funnel-Request": "?1"}, "a@x.com")[0])


class RepoHygieneTest(unittest.TestCase):
    """Files that git would publish must not contain this machine's personal details or the API key.

    The words to look for are collected at run time (user name, computer name, tailnet name and owner, git e-mail,
    the API key), so the test itself contains nothing personal.
    """

    def personal_terms(self) -> list[str]:
        terms = []
        for v in (os.environ.get("USERNAME"), os.environ.get("USER"), os.environ.get("COMPUTERNAME")):
            if v and len(v) >= 4 and v.lower() not in ("user", "admin", "runner", "tester"):
                terms.append(v)
        st = auth.tailscale_status()
        if st.get("dns_name"):
            terms += [st["dns_name"], st["dns_name"].split(".")[1] if st["dns_name"].count(".") >= 2 else ""]
        if st.get("owner"):
            terms.append(st["owner"])
        mail = subprocess.run(["git", "config", "user.email"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
        if mail:
            terms.append(mail)
        from snaplab.llm import get_api_key
        key = get_api_key("SNAPSCRIBE_API_KEY")
        if key:
            terms.append(key)
        return [t.lower() for t in terms if t and len(t) >= 4]

    def test_no_personal_strings_in_tracked_files(self):
        p = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True)
        if p.returncode != 0:
            self.skipTest("not a git checkout")
        terms = self.personal_terms()
        hits = []
        for name in p.stdout.splitlines():
            f = ROOT / name
            if not f.is_file() or f.suffix.lower() in (".png", ".webp", ".jpg", ".ico"):
                continue
            text = f.read_text(encoding="utf-8", errors="ignore").lower()
            for i, word in enumerate(terms):
                if word in text:
                    hits.append(f"{name}: personal term #{i}")
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
