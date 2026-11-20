"""Tool plugins: add a tool for bots by dropping one Python file into a tools folder (no edits to SnapLab).

Folders (config "tool_dirs"): by default `plugins/tools/` in the repository and `<data>/plugins/tools/`.
A plugin file defines:

    TOOL = {
        "name": "calculator",                      # lowercase letters, digits, "_" (must not clash with built-ins)
        "description": "What it does, for the model.",
        "parameters": {"type": "object", "properties": {...}, "required": [...]},   # JSON schema
        "scope": ["chat", "session"],              # where bots may use it (default: chat)
        "approval": "never",                       # never | after_web | always   (proposal card before it runs)
        "orchestrator_only": False,
        "label": {"en": "Calculated", "ja": "計算した"},   # optional: the verb on the phone's tool chip
        "untrusted": False,                        # True if the result comes from outside (web etc.): wrapped as data
    }

    def run(ctx, **args) -> str | dict:            # dict results are sent to the model as JSON
        ...

`ctx` gives: ctx.app, ctx.bot, ctx.room (chat) or ctx.session (session), ctx.folder (the session folder, sessions
only), ctx.lang, ctx.read_web, ctx.guard. Plugins run inside the SnapLab process with its permissions, like any
Python you install: only add code you trust. Bots still cannot run code through plugins unless a plugin does so,
so keep plugins free of shell or code execution (use a Codex session for that).
"""
from __future__ import annotations

import importlib.util
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import ROOT

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,40}$")
APPROVALS = ("never", "after_web", "always")
SCOPES = ("chat", "session")


@dataclass
class PluginTool:
    name: str
    description: str
    parameters: dict
    run: object
    scope: tuple = ("chat",)
    approval: str = "never"
    orchestrator_only: bool = False
    label: dict = field(default_factory=dict)
    untrusted: bool = False
    path: str = ""

    def schema(self) -> dict:
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": self.parameters}}


@dataclass
class PluginContext:
    app: object
    bot: dict
    room: dict | None
    session: dict | None
    folder: Path | None
    lang: str
    read_web: bool

    @property
    def guard(self):
        return self.app.guard


class PluginError(ValueError):
    pass


def validate(tool: dict, run, path: str, reserved: set[str]) -> PluginTool:
    if not isinstance(tool, dict):
        raise PluginError("TOOL must be a dict")
    name = tool.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise PluginError("TOOL['name'] must be 3-41 lowercase letters, digits or _")
    if name in reserved:
        raise PluginError(f"the name {name!r} is already used by another tool")
    if not callable(run):
        raise PluginError("the plugin needs a run(ctx, **args) function")
    desc = tool.get("description")
    if not isinstance(desc, str) or not desc.strip():
        raise PluginError("TOOL['description'] is required")
    params = tool.get("parameters") or {"type": "object", "properties": {}}
    if not isinstance(params, dict) or params.get("type") != "object" or not isinstance(params.get("properties", {}), dict):
        raise PluginError("TOOL['parameters'] must be a JSON schema object with properties")
    scope = tuple(tool.get("scope") or ("chat",))
    if not scope or any(s not in SCOPES for s in scope):
        raise PluginError(f"TOOL['scope'] must be a list of {SCOPES}")
    approval = tool.get("approval", "never")
    if approval not in APPROVALS:
        raise PluginError(f"TOOL['approval'] must be one of {APPROVALS}")
    label = tool.get("label") or {}
    if not isinstance(label, dict):
        raise PluginError("TOOL['label'] must be a dict of language -> text")
    return PluginTool(name=name, description=desc.strip()[:1000], parameters=params, run=run, scope=scope,
                      approval=approval, orchestrator_only=bool(tool.get("orchestrator_only")),
                      label={k: str(v)[:40] for k, v in label.items()}, untrusted=bool(tool.get("untrusted")), path=path)


def tool_dirs(cfg: dict) -> list[Path]:
    dirs = cfg.get("tool_dirs")
    if dirs is None:
        dirs = [str(ROOT / "plugins" / "tools"), str(Path(cfg["data_dir"]) / "plugins" / "tools")]
    out = []
    for d in dirs:
        p = Path(d)
        if not p.is_absolute():
            p = ROOT / p
        out.append(p)
    return out


def load(cfg: dict, reserved: set[str], log=print) -> dict[str, PluginTool]:
    """Load every *.py plugin from the tool folders. Broken plugins are skipped and reported in the log."""
    found: dict[str, PluginTool] = {}
    for d in tool_dirs(cfg):
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.py")):
            if f.name.startswith("_"):
                continue
            try:
                spec = importlib.util.spec_from_file_location(f"snaplab_plugin_{f.stem}", f)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                t = validate(getattr(mod, "TOOL", None), getattr(mod, "run", None), str(f), reserved | set(found))
                found[t.name] = t
            except Exception as e:  # noqa: BLE001 - one broken plugin must not stop SnapLab
                log(f"tool plugin {f.name} not loaded: {e}")
    return found
