"""After UI key registration: four small calls for chat, tools and SnapCode translation.

python tools/check_provider.py --config PATH --model REGISTRY_NAME
Uses synthetic messages only. Reads the selected provider credential inside LLM/registry.
Never prints credentials, response text or exception bodies. No automatic retries.
"""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from snaplab import config
from snaplab.models import ModelRegistry
from snaplab.slots import Slots
from snaplab.proxy import Translator


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--ssh-host", help="Also run an isolated real SnapCode with at most four additional model calls")
    p.add_argument("--ssh-exe", default="ssh")
    a = p.parse_args()
    cfg = config.load(a.config)
    shared = Slots(cfg["slots"]["dir"], cfg["slots"]["count"])
    registry = ModelRegistry(cfg, shared)
    spec = registry.spec(a.model)
    if "snapscribe" in spec["base_url"].lower() or spec["api_key_env"] == "SNAPSCRIBE_API_KEY":
        print(json.dumps({"ok": False, "reason": "Use the separately authorized snaplib probe for Snapscribe."}))
        return 1
    report = {"model": a.model, "calls": 0, "ok": False}
    try:
        client = registry.get(a.model)
        r = client.stream_chat([{"role": "user", "content": "Reply with OK."}], effort="off", max_tokens=128)
        report.update(calls=1, chat=bool(r.content))
        tool = {"type": "function", "function": {"name": "echo", "description": "Return an integer.",
            "parameters": {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]}}}
        messages = [{"role": "user", "content": "Call echo with value 7. Then tell me its result."}]
        report["calls"] += 1
        r = client.stream_chat(messages, tools=[tool], effort="off", max_tokens=256)
        assert len(r.tool_calls) == 1
        call = r.tool_calls[0]
        assert call["name"] == "echo" and json.loads(call["arguments"])["value"] == 7
        messages += [{"role": "assistant", "content": r.content, "tool_calls": [{"id": call["id"], "type": "function",
                      "function": {"name": "echo", "arguments": call["arguments"]}}]},
                     {"role": "tool", "tool_call_id": call["id"], "content": "7"}]
        report["calls"] += 1
        report["tools"] = "7" in client.stream_chat(messages, effort="off", max_tokens=128).content
        with tempfile.TemporaryDirectory(prefix="snaplab-provider-check-") as temp:
            import secrets
            token = secrets.token_urlsafe(32)
            proxy = Translator(port=0, api_cfg=cfg["api"], slots=shared, key_fn=lambda: None, token=token,
                log_path=Path(temp)/"proxy.jsonl", resolve=lambda _: {**spec, "key": registry.key(a.model), "slots": registry.slots_for(a.model)})
            assert proxy.start()
            try:
                report["calls"] += 1
                body = {"model": a.model, "input": [{"role": "user", "content": "Reply with OK."}], "stream": False, "max_output_tokens": 128,
                        "reasoning": {"effort": "none"}}
                request = urllib.request.Request(f"http://127.0.0.1:{proxy.port}/v1/responses", data=json.dumps(body).encode(),
                    headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
                with urllib.request.urlopen(request, timeout=120) as response:
                    result = json.load(response)
                report["snapcode_translation"] = result.get("status") == "completed" and bool(result.get("output"))
            finally:
                proxy.stop()
        report["ok"] = all(report.get(k) for k in ("chat", "tools", "snapcode_translation"))
        if a.ssh_host:
            report["ssh"] = check_ssh(a, cfg, spec)
            report["ok"] &= report["ssh"]["ok"]
    except Exception as exc:
        from snaplab.llm import error_code
        report["ok"] = False
        report["error_code"] = error_code(exc)
    print(json.dumps(report))
    return 0 if report["ok"] else 1


def check_ssh(args, cfg, spec):
    """Fresh application, synthetic task and selected credential only; no production data."""
    from unittest.mock import patch
    from snaplab.app import App
    from snaplab.sessions import Control
    from snaplab.llm import ApiError
    calls = 0
    with tempfile.TemporaryDirectory(prefix="snaplab-provider-ssh-") as temp:
        selected = cfg["models"][args.model]
        provider = cfg["providers"][selected["provider"]]
        isolated = config.load(Path(temp)/"no-config.json", overrides={
            "data_dir": temp, "local_mode": True, "api": {"base_url": spec["base_url"], "model": spec["model"],
            "api_key_env": spec["api_key_env"], "effort_style": spec["effort_style"]},
            "providers": {selected["provider"]: provider}, "models": {args.model: selected},
            "routing": {k: args.model for k in ("default", "chat", "session", "codex", "background")},
            "sessions": {"report_by_bot": False, "codex_timeout_min": 3},
            "codex": {"backend": "ssh", "remote": {"host": args.ssh_host, "ssh_exe": args.ssh_exe, "desktop": False}}})
        app = App(isolated, start_translator=False, owner="synthetic@example.invalid")
        # This test uses only the selected provider, including at exit-guard boundaries.
        app.guard.extra_env = [spec["api_key_env"]]
        original = app._translator_target
        def target(ref):
            nonlocal calls
            if calls >= 4:
                raise ValueError("Probe call budget reached.")
            calls += 1
            return original(ref)
        app._translator_target = target
        try:
            bot = app.onboard(lang="en", name="Provider check", color="#2F6FED", tone="polite", tone_custom="", call_user="Tester")["bot"]
            with patch("snaplab.gitops.available", return_value=False):
                project = app.create_project(name="Synthetic provider acceptance", bot=bot["id"])
            with patch.object(app.sessions, "_start"):
                session = app.sessions.create(project, "codex", "Run python3 -c 'print(2+2)' once and report the result. Do not use the network.", bot=bot["id"], model=args.model)
            app.sessions._turn(session["id"], session["goal"], True, Control(app.sessions, session["id"]))
            result = app.store.session(session["id"])
            return {"ok": result["status"] == "review", "status": result["status"], "calls": calls}
        finally:
            app.speech.close(); app.archive.close()


if __name__ == "__main__":
    raise SystemExit(main())
