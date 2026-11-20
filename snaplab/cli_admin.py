"""Settings > Advanced: AI apps that run on this PC with the user's own sign-in (Claude Code, Codex CLI).

SnapLab never reads, stores or forwards these logins. It runs the unmodified program the user installed and
signed in to through the company's own flow (claude_cli.py, codex_cli.py), so use counts against the user's plan
and follows that company's terms. Gemini is not offered here: Google's terms do not allow other apps to use the
Gemini CLI sign-in, so Gemini is added with an API key (presets.py) instead.

On Windows "Install" and "Sign in" open a PowerShell window on the PC's desktop that runs the official command; on
Linux and macOS the page shows the command to run in a terminal. Nothing here is offered to a phone.
"""
from __future__ import annotations

import copy
import os
import re
import secrets
import subprocess

from . import claude_cli, codex_cli
from .models import CLAUDE_CLI, CODEX_CLI

APPS = {
    "claude": {"kind": CLAUDE_CLI, "label": "Claude", "program": "Claude Code", "module": claude_cli,
               "cmd": "claude", "models": ["sonnet", "opus", "haiku"], "context_limit": 200000,
               "install": {"nt": "irm https://claude.ai/install.ps1 | iex",
                           "posix": "curl -fsSL https://claude.ai/install.sh | bash"},
               "signin": ["auth", "login"], "docs": "https://code.claude.com/docs/en/setup"},
    "codex": {"kind": CODEX_CLI, "label": "ChatGPT", "program": "Codex CLI", "module": codex_cli,
              "cmd": "codex", "models": [], "context_limit": 272000,
              "install": {"nt": "irm https://chatgpt.com/codex/install.ps1 | iex",
                          "posix": "curl -fsSL https://chatgpt.com/codex/install.sh | sh"},
              "signin": ["login"], "docs": "https://developers.openai.com/codex/cli"},
}
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,79}")
PLATFORM = "nt" if os.name == "nt" else "posix"


def _app(which: str) -> dict:
    if which not in APPS:
        raise LookupError("no such app")
    return APPS[which]


def view(app, fresh: bool = False) -> dict:
    out = []
    for key, a in APPS.items():
        st = a["module"].status(None, fresh)
        added = [{"name": n, "label": m.get("label") or n, "model": m.get("model") or ""}
                 for n, m in app.cfg["models"].items()
                 if (app.cfg["providers"].get(m.get("provider")) or {}).get("kind") == a["kind"]]
        signin = " ".join([a["cmd"], *a["signin"]])
        out.append({"id": key, "label": a["label"], "program": a["program"], **st, "models": a["models"],
                    "install": a["install"][PLATFORM], "signin": signin, "docs": a["docs"], "added": added,
                    "can_open": os.name == "nt"})
    return {"apps": out}


def open_window(app, which: str, step: str) -> dict:
    """Windows only: a PowerShell window on the user's desktop runs the official install or sign-in command."""
    a = _app(which)
    if os.name != "nt":
        raise ValueError("Run the command in a terminal on this computer.")
    if step == "install":
        command = a["install"]["nt"]
    elif step == "signin":
        exe = a["module"].find_exe(None)
        if not exe:
            raise ValueError(a["program"] + " is not installed on this PC yet.")
        command = "& '" + exe.replace("'", "''") + "' " + " ".join(a["signin"])
    else:
        raise ValueError("unknown step")
    note = "Write-Host 'When this is done, close this window and press Check again in SnapLab.'"
    subprocess.Popen(["powershell.exe", "-NoProfile", "-NoExit", "-ExecutionPolicy", "Bypass", "-Command",
                      command + "; " + note], creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
                     cwd=os.path.expanduser("~"))
    return {"opened": True}


def _test(app, a: dict, model: str) -> None:
    """One short turn without tools, so a wrong model name or a missing sign-in shows before saving."""
    from .llm import ApiError
    spec = {"name": "cli-test", "model": model}
    client = (claude_cli.ClaudeCLI if a["kind"] == CLAUDE_CLI else codex_cli.CodexCLI)(
        spec, {"timeout_s": 180}, app.cfg["data_dir"], None, usage=None, redactor=app.redactor)
    try:
        reply = client.stream_chat([{"role": "system", "content": "You are being tested. Reply with the single word OK."},
                                    {"role": "user", "content": "Reply with OK."}], effort="low", source="setup")
    except ApiError as e:
        raise ValueError(a["program"] + " did not answer: " + str(e)[:300]) from None
    if not (reply.content or "").strip():
        raise ValueError(a["program"] + " answered with nothing. Check the model name.")


def add(app, which: str, model: str = "") -> dict:
    """Test the app, then add it as a model that bots can use (Settings > Models)."""
    from . import setup
    a = _app(which)
    model = str(model or "").strip()
    if model and not MODEL_RE.fullmatch(model):
        raise ValueError("Enter a model name such as " + (a["models"][0] if a["models"] else "gpt-5") + ", or leave it empty.")
    st = a["module"].status(None, fresh=True)
    if not st["installed"]:
        raise ValueError(a["program"] + " is not installed on this PC yet.")
    if not st["signed_in"]:
        raise ValueError("Sign in to " + a["program"] + " first.")
    _test(app, a, model)
    with setup._LOCK:
        providers = copy.deepcopy(app.cfg["providers"])
        models = copy.deepcopy(app.cfg["models"])
        pname = next((n for n, p in providers.items() if p.get("kind") == a["kind"] and not p.get("exe")), None)
        if not pname:
            pname = which + "-cli"
            while pname in providers:
                pname = which + "-cli-" + secrets.token_hex(2)
            providers[pname] = {"kind": a["kind"], "label": a["program"], "timeout_s": 0}
        name = which + "-" + secrets.token_hex(3)
        models[name] = {"provider": pname, "model": model, "context_limit": a["context_limit"],
                        "label": a["label"] + " (" + (model or a["program"] + " default") + ")",
                        "capabilities": {"vision": True, "tools": True}}
        result = setup._persist(app, providers, models, copy.deepcopy(app.cfg["routing"]))
    return {**result, "added": name}
