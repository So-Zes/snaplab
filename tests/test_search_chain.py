"""Web search tries its sources in order and a key can be saved in Settings (2026-11-21).

DuckDuckGo started answering with a human check on a 5G home connection, and Tavily's keyless access was at its
daily limit for the same shared address. Search then stopped for 30 minutes at a time."""
import json
import socket
import time
import unittest
from unittest.mock import patch

from snaplab import websearch
from snaplab.websearch import Searcher, SearchError

FAKE_KEY = "tvly-synthetic-test-key"
RESULTS = [{"title": "Result", "url": "https://example.com/", "snippet": "text"}]


def captcha(*a, **k):
    raise SearchError("captcha", "DuckDuckGo asked for a human check (CAPTCHA). Stopped without trying to get around it.")


def keys(tavily=None, brave=None):
    return patch("snaplab.llm.get_api_key", side_effect=lambda n: {"TAVILY_API_KEY": tavily, "BRAVE_API_KEY": brave}.get(n))


class ChainTest(unittest.TestCase):
    def test_order_without_and_with_keys(self):
        with keys():
            self.assertEqual([n for n, _ in Searcher({}).chain()], ["duckduckgo", "tavily-keyless"])
        with keys(tavily=FAKE_KEY, brave="b"):
            self.assertEqual([n for n, _ in Searcher({}).chain()], ["tavily", "brave", "duckduckgo", "tavily-keyless"])
            self.assertEqual(Searcher({"provider": "duckduckgo"}).chain(), [("duckduckgo", None)])

    def test_captcha_moves_on_to_the_next_source_and_asks_it_again_next_time(self):
        s = Searcher({})
        with keys(), patch.object(websearch, "search_duckduckgo", side_effect=captcha) as ddg, \
                patch.object(websearch, "search_tavily", return_value=RESULTS) as tav:
            out = s.search("bug bounty")
            self.assertEqual(out["provider"], "tavily-keyless")
            self.assertEqual(tav.call_args.args[2], None)            # keyless: no key sent
            self.assertIn("human check", out["skipped"][0])
            s.search("again")
            self.assertEqual(ddg.call_count, 2)                      # a CAPTCHA does not pause the source
        self.assertNotIn("duckduckgo", s.paused_until)

    def test_all_refused_says_when_and_how_to_avoid_it(self):
        s = Searcher({})
        limit = SearchError("limit", "Tavily says its free keyless limit is used up for this connection", 591)
        with keys(), patch.object(websearch, "search_duckduckgo", side_effect=captcha), \
                patch.object(websearch, "search_tavily", side_effect=limit):
            with self.assertRaises(SearchError) as cm:
                s.search("bug bounty")
            self.assertEqual(cm.exception.kind, "captcha")
            self.assertIn("Settings > Web search", cm.exception.message)
            self.assertEqual(s.paused_minutes(), 0)                  # DuckDuckGo is asked again next time
            self.assertGreater(s.paused_until["tavily-keyless"], time.time() + 9 * 60)
            with self.assertRaises(SearchError) as cm2:
                s.search("again")
            self.assertIn("paused for about", cm2.exception.message)

    def test_a_key_is_used_first(self):
        with keys(tavily=FAKE_KEY), patch.object(websearch, "search_tavily", return_value=RESULTS) as tav, \
                patch.object(websearch, "search_duckduckgo", side_effect=AssertionError("not reached")):
            out = Searcher({}).search("q")
        self.assertEqual(out["provider"], "tavily")
        self.assertEqual(tav.call_args.args[2], FAKE_KEY)
        self.assertNotIn("skipped", out)

    def test_selected_provider_without_key_is_a_config_error(self):
        with keys(), self.assertRaises(SearchError) as cm:
            Searcher({"provider": "tavily"}).search("q")
        self.assertEqual(cm.exception.kind, "config")

    def test_view_reports_order_and_pauses(self):
        s = Searcher({})
        s.paused_until["duckduckgo"] = time.time() + 590
        with keys():
            v = s.view()
        self.assertEqual([o["name"] for o in v["order"]], ["duckduckgo", "tavily-keyless"])
        self.assertEqual(v["order"][0]["paused_minutes"], 10)
        self.assertEqual(v["paused_minutes"], 0)                     # keyless Tavily can still be tried
        self.assertEqual(v["keys"], {"tavily": False, "brave": False})


class TavilyRefusalTest(unittest.TestCase):
    def test_keyless_limit_uses_retry_after_and_passes_on_what_the_site_said(self):
        body = json.dumps({"error": {"code": "daily_cap_reached", "message": "pay via x402 agentic payment",
                                     "next_actions": [{"type": "agentic_payment"}]}}).encode()
        with patch.object(websearch, "_http", return_value=(429, {"retry-after": "591"}, body, "")) as http:
            with self.assertRaises(SearchError) as cm:
                websearch.search_tavily("q", 3, None)
        sent = http.call_args.kwargs["headers"]
        self.assertEqual(sent.get("X-Tavily-Access-Mode"), "keyless")
        self.assertNotIn("Authorization", sent)
        self.assertEqual(cm.exception.kind, "limit")
        self.assertEqual(cm.exception.retry_after, 591)
        self.assertIn("pay via x402 agentic payment", cm.exception.message)

    def test_bad_key_is_an_http_error(self):
        with patch.object(websearch, "_http", return_value=(401, {}, b"{}", "")):
            with self.assertRaises(SearchError) as cm:
                websearch.search_tavily("q", 3, FAKE_KEY)
        self.assertEqual(cm.exception.kind, "http")


class ReadPageNameTest(unittest.TestCase):
    def test_a_name_that_does_not_exist_is_not_called_private(self):
        with patch.object(websearch.socket, "getaddrinfo", side_effect=socket.gaierror(11001, "getaddrinfo failed")):
            with self.assertRaises(SearchError) as cm:
                websearch.read_page("https://no-such-site.example/")
        self.assertEqual(cm.exception.kind, "network")
        self.assertIn("was not found", cm.exception.message)
        self.assertNotIn("tailnet", cm.exception.message)

    def test_local_names_are_still_refused(self):
        with self.assertRaises(SearchError) as cm:
            websearch.read_page("http://localhost:8787/")
        self.assertEqual(cm.exception.kind, "blocked")


class SaveKeyTest(unittest.TestCase):
    def setUp(self):
        class Guard:
            def __init__(self):
                self.added = []

            def add(self, v):
                self.added.append(v)

        class App:
            pass
        self.app = App()
        self.app.guard = Guard()
        self.app.searcher = Searcher({})

    def test_a_working_key_is_tested_then_saved_under_the_name_search_reads(self):
        from snaplab import setup
        with patch.object(websearch, "search_tavily", return_value=RESULTS) as probe, \
                patch("snaplab.credentials.write_credential") as save, keys():
            out = setup.search_key(self.app, {"provider": "tavily", "key": " " + FAKE_KEY + " "})
        self.assertEqual(probe.call_args.args[2], FAKE_KEY)
        save.assert_called_once_with("TAVILY_API_KEY", FAKE_KEY)
        self.assertIn(FAKE_KEY, self.app.guard.added)
        self.assertIn("order", out["search"])

    def test_a_refused_key_is_not_saved(self):
        from snaplab import setup
        bad = SearchError("http", "Tavily refused the API key (HTTP 401)")
        with patch.object(websearch, "search_tavily", side_effect=bad), \
                patch("snaplab.credentials.write_credential") as save:
            with self.assertRaises(ValueError) as cm:
                setup.search_key(self.app, {"provider": "tavily", "key": FAKE_KEY})
        save.assert_not_called()
        self.assertNotIn(FAKE_KEY, str(cm.exception))

    def test_remove_and_unknown_provider(self):
        from snaplab import setup
        with patch("snaplab.credentials.delete_credential") as delete, keys():
            setup.search_key(self.app, {"provider": "brave", "action": "remove"})
        delete.assert_called_once_with("BRAVE_API_KEY")
        with self.assertRaises(ValueError):
            setup.search_key(self.app, {"provider": "google", "key": "x"})


if __name__ == "__main__":
    unittest.main()
