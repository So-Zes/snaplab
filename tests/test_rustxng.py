"""RustXNG as the last web search source (2026-11-21): the pinned download is checked, a running one is reused,
and the bot is told what it searched."""
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from snaplab import rustxng, websearch
from snaplab.rustxng import RustXNG
from snaplab.websearch import Searcher, SearchError

RESULTS = [{"title": "Result", "url": "https://example.com/", "snippet": "text"}]


def no_keys():
    return patch("snaplab.llm.get_api_key", return_value=None)


class Ready(RustXNG):
    def __init__(self):
        super().__init__({})
        self.ready, self.state = True, "ready"


class FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class ChainTest(unittest.TestCase):
    def test_rustxng_comes_last_and_only_when_it_runs(self):
        s = Searcher({})
        with no_keys():
            s.rustxng = RustXNG({})                      # installed or not, it is not running
            self.assertEqual([n for n, _ in s.chain()], ["duckduckgo", "tavily-keyless"])
            s.rustxng = Ready()
            self.assertEqual(s.chain()[-1], ("rustxng", "http://127.0.0.1:29483"))
            s.rustxng.enabled = False
            self.assertNotIn("rustxng", [n for n, _ in s.chain()])

    def test_when_the_others_refuse_rustxng_answers_and_the_bot_is_told(self):
        s = Searcher({})
        s.rustxng = Ready()
        limit = SearchError("limit", "Tavily says its free keyless limit is used up for this connection", 600)
        with no_keys(), patch.object(websearch, "search_duckduckgo", side_effect=SearchError("captcha", "DuckDuckGo asked for a human check")), \
                patch.object(websearch, "search_tavily", side_effect=limit), \
                patch.object(websearch, "search_rustxng", return_value=RESULTS) as rx:
            out = s.search("bug bounty")
        self.assertEqual(out["provider"], "rustxng")
        self.assertEqual(rx.call_args.args[2], "http://127.0.0.1:29483")
        self.assertEqual(len(out["skipped"]), 2)
        self.assertIn("open indexes", out["note"])

    def test_the_json_api_is_read(self):
        body = json.dumps({"results": [
            {"title": "Immunefi  Top 10", "url": "https://immunefi.com/immunefi-top-10/", "content": "Smart contracts", "engine": "mwmbl"},
            {"title": "no link", "url": "javascript:alert(1)", "content": ""}]}).encode()
        with patch.object(websearch._local_opener, "open", return_value=FakeResponse(body)) as op:
            out = websearch.search_rustxng("immunefi", 5, "http://127.0.0.1:29483/")
        url = op.call_args.args[0].full_url
        self.assertTrue(url.startswith("http://127.0.0.1:29483/search?"))
        self.assertIn("format=json", url)
        self.assertEqual(out, [{"title": "Immunefi Top 10", "url": "https://immunefi.com/immunefi-top-10/", "snippet": "Smart contracts"}])

    def test_not_running_is_a_network_error(self):
        with patch.object(websearch._local_opener, "open", side_effect=OSError("refused")):
            with self.assertRaises(SearchError) as cm:
                websearch.search_rustxng("q", 5, "http://127.0.0.1:29483")
        self.assertEqual(cm.exception.kind, "network")


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rustxng-test-")
        self.env = patch.dict(os.environ, {"SNAPLAB_RUSTXNG_HOME": self.tmp})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.payload = b"synthetic rustxng binary"

    def asset(self, digest):
        return patch.object(rustxng, "asset", return_value=("rustxng-test-asset", digest))

    def test_a_download_that_does_not_match_is_deleted(self):
        rx = RustXNG({})
        with self.asset("0" * 64), patch.object(rustxng.urllib.request, "urlopen", return_value=FakeResponse(self.payload)), \
                patch.object(RustXNG, "start") as start:
            self.assertFalse(rx.install())
        start.assert_not_called()
        self.assertEqual(rx.state, "error")
        self.assertIn("SHA-256", rx.error)
        self.assertEqual(list(Path(self.tmp).rglob("rustxng*")), [])

    def test_a_matching_download_is_kept_and_started(self):
        rx = RustXNG({})
        digest = hashlib.sha256(self.payload).hexdigest()
        with self.asset(digest), patch.object(rustxng.urllib.request, "urlopen", return_value=FakeResponse(self.payload)) as get, \
                patch.object(RustXNG, "start", return_value=True) as start:
            self.assertTrue(rx.install())
            exe = rustxng.exe_path()
        self.assertTrue(str(exe).startswith(self.tmp))
        self.assertIn("/RustXNG/RustXNG/releases/download/v0.32.0/rustxng-test-asset", get.call_args.args[0].full_url)
        self.assertEqual(exe.read_bytes(), self.payload)
        self.assertEqual(json.loads((exe.parent / "SOURCE.json").read_text())["sha256"], digest)
        start.assert_called_once()

    def test_one_that_already_answers_is_used_without_starting_another(self):
        rx = RustXNG({})
        with patch.object(rustxng, "healthy", return_value=True), patch.object(rustxng, "running_profile", return_value="full"), \
                patch.object(rustxng.subprocess, "Popen") as popen:
            self.assertTrue(rx.start())
        popen.assert_not_called()
        self.assertTrue(rx.usable())
        self.assertEqual(rx.profile, "full")                      # the default profile

    def test_one_running_with_another_profile_is_started_again(self):
        rx = RustXNG({"profile": "full"})
        with self.asset("0" * 64):
            exe = rustxng.exe_path()
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_bytes(b"x")
        answers = iter([True, False, True])
        with self.asset("0" * 64), patch.object(rustxng, "healthy", side_effect=lambda *a, **k: next(answers, True)), \
                patch.object(rustxng, "running_profile", return_value="standard"), \
                patch.object(rustxng, "stop_on_port") as stop, patch.object(rustxng.subprocess, "Popen") as popen:
            self.assertTrue(rx.start())
        stop.assert_called_once_with("29483")
        self.assertIn("full", popen.call_args.args[0])

    def test_profile_from_settings_and_unknown_profiles_refused(self):
        rx = RustXNG({})
        with patch.object(RustXNG, "start_in_background") as again:
            rx.set_profile("standard")
        self.assertEqual(rx.profile, "standard")
        self.assertEqual(rx.view()["profiles"], ["full", "standard", "clean"])
        with self.assertRaises(ValueError):
            rx.set_profile("anything")

    def test_running_profile_is_read_from_the_engines(self):
        def config(names):
            return FakeResponse(json.dumps({"engines": [{"name": n, "enabled": True} for n in names]}).encode())
        for names, want in ((["mwmbl", "wikipedia", "naver", "keenable"], "full"), (["mwmbl", "wikipedia"], "standard"),
                            (["wikipedia"], "clean")):
            with patch("urllib.request.OpenerDirector.open", return_value=config(names)):
                self.assertEqual(rustxng.running_profile("http://127.0.0.1:29483"), want)

    def test_not_installed_stays_absent_and_never_downloads(self):
        rx = RustXNG({})
        with self.asset("0" * 64), patch.object(rustxng, "healthy", return_value=False), \
                patch.object(rustxng.urllib.request, "urlopen", side_effect=AssertionError("no download")):
            self.assertFalse(rx.start())
        self.assertEqual(rx.state, "absent")

    def test_snaplab_start_installs_when_missing_and_starts_when_present(self):
        rx = RustXNG({})
        with self.asset("0" * 64), patch.object(RustXNG, "install_in_background") as install, \
                patch.object(RustXNG, "start_in_background") as start:
            rx.ensure_in_background()
            install.assert_called_once()
            start.assert_not_called()
            exe = rustxng.exe_path()
            exe.parent.mkdir(parents=True, exist_ok=True)
            exe.write_bytes(b"x")
            rx.ensure_in_background()
            start.assert_called_once()
        off = RustXNG({"enabled": False})
        with self.asset("0" * 64), patch.object(RustXNG, "install_in_background") as install:
            off.ensure_in_background()
        install.assert_not_called()

    def test_turned_off_in_config(self):
        rx = RustXNG({"enabled": False})
        self.assertFalse(rx.start())
        self.assertEqual(rx.view()["state"], "off")


class SettingsTest(unittest.TestCase):
    def test_settings_installs_in_the_background(self):
        from snaplab import setup

        class App:
            pass
        app = App()
        app.searcher = Searcher({})
        app.searcher.rustxng = RustXNG({})
        import threading
        called = threading.Event()
        with patch.object(RustXNG, "install", side_effect=lambda *a: called.set()), no_keys():
            out = setup.search_key(app, {"action": "rustxng"})
            self.assertTrue(called.wait(5))              # the thread ran the (fake) install before the patch ends
        self.assertEqual(out["search"]["rustxng"]["state"], "installing")
        with no_keys():
            self.assertIn("order", setup.search_key(app, {"action": "view"})["search"])


if __name__ == "__main__":
    unittest.main()
