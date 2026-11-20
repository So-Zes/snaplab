"""Run synthetic tests inside workspace-write, with no Git commits or network tests."""
import argparse
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from helpers import fake_credentials
SKIP = {
    "test_server.ServerTest": "loopback HTTP is unavailable in the sandbox",
    "test_server.TranslatorTest": "loopback HTTP is unavailable in the sandbox",
    "test_extend.ModelRegistryTest.test_bot_model_choice_is_used_for_its_turns": "loopback HTTP",
    "test_extend.ModelRegistryTest.test_cut_stream_raises_with_the_partial_text": "loopback HTTP",
    "test_extend.ModelRegistryTest.test_second_provider_is_called_with_its_own_model_key_and_effort_style": "loopback HTTP",
    "test_extend.ModelRegistryTest.test_translator_routes_codex_to_the_chosen_model": "loopback HTTP",
    "test_core.SessionsTest.test_worktree_result_card_and_merge": "Git commits are reserved for the coordinator",
    "test_core.GuardTest.test_secret_in_session_files_is_removed_before_commit": "Git commits are reserved for the coordinator",
    "test_sandbox.SandboxReadTest": "nested Windows sandbox and Git",
}


def cases(suite):
    for case in suite:
        if isinstance(case, unittest.TestSuite):
            yield from cases(case)
        else:
            yield case


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pattern", default="test*.py")
    args = parser.parse_args()
    scratch = ROOT / ".tmp-offline-tests"
    scratch.mkdir(exist_ok=True)
    tempfile.tempdir = str(scratch)
    with fake_credentials(), patch("snaplab.gitops.available", return_value=False), \
         patch("snaplab.auth.tailscale_status", return_value={}):
        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern=args.pattern)
        for case in cases(suite):
            reason = next((why for name, why in SKIP.items() if case.id().startswith(name)), None)
            if reason:
                if case.id().rsplit(".", 1)[0] in SKIP:
                    unittest.skip(reason)(type(case))
                else:
                    method = getattr(type(case), case._testMethodName)
                    setattr(type(case), case._testMethodName, unittest.skip(reason)(method))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
