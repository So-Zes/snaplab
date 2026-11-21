"""Providers and models: any number of OpenAI-compatible endpoints, chosen per bot and per kind of call.

config.json:

    "providers": {
      "snapscribe": {"base_url": "<see presets.py>", "api_key_env": "SNAPSCRIBE_API_KEY",
                     "slots": "shared"},                       # "shared" = the global slot folder/count
      "other":      {"base_url": "https://api.example.com/v1", "api_key_env": "OTHER_API_KEY",
                     "slots": {"count": 4}}                     # its own limit (data/locks/<provider>)
    },
    "models": {
      "qwen":  {"provider": "snapscribe", "model": "qwen3.8-27b", "context_limit": 131072,
                "effort_style": "chat_template_kwargs", "label": "Snapscribe 1"},
      "fast":  {"provider": "other", "model": "some-model", "effort_style": "reasoning_effort"}
    },
    "routing": {"default": "qwen", "chat": "qwen", "session": "qwen", "codex": "qwen"}

- Keys are never in the config: each provider names the environment variable that holds its key.
- effort_style says how thinking depth is sent: "chat_template_kwargs" (vLLM-style servers, the
  Snapscribe default), "reasoning_effort" (OpenAI-style top-level field) or "none".
- A bot can pick a model (bot["model"]); otherwise routing decides by kind (chat, session, codex).
- The old single "api" block still works: it becomes the provider "snapscribe" and the model "qwen".
"""
from __future__ import annotations

from pathlib import Path

from .llm import LLM, get_api_key
from .presets import SNAPSCRIBE_LABEL
from .slots import Slots

KINDS = ("chat", "session", "codex", "background")
CLAUDE_CLI = "claude_cli"         # provider kind: the bot's turn runs in Claude Code (snaplab/claude_cli.py)
CODEX_CLI = "codex_cli"           # provider kind: the bot's turn runs in `codex exec` (snaplab/codex_cli.py)
CLI_KINDS = (CLAUDE_CLI, CODEX_CLI)
EFFORT_STYLES = ("chat_template_kwargs", "reasoning_effort", "none")


def derive(cfg: dict) -> None:
    """Fill providers/models/routing from the legacy "api" block when they are not given (in place)."""
    api = cfg.get("api") or {}
    providers = cfg.setdefault("providers", {}) or {}
    models = cfg.setdefault("models", {}) or {}
    qwen = "qwen" in str(api.get("model") or "").lower()
    provider_name, model_name = ("snapscribe", "qwen") if qwen or cfg.get("_legacy_api_names") else ("default", "default")
    same = [n for n, m in models.items() if isinstance(m, dict) and m.get("model") == api.get("model")
            and (providers.get(m.get("provider")) or {}).get("base_url") == api.get("base_url")]
    if same:
        # The setup page writes the model as a provider and model and also as the old "api" block; the block must
        # not add a second copy of the same model.
        routing = cfg.setdefault("routing", {}) or {}
        routing.setdefault("default", same[0])
        for k in KINDS:
            routing.setdefault(k, routing["default"])
        cfg["providers"], cfg["models"], cfg["routing"] = providers, models, routing
        return
    providers.setdefault(provider_name, {
        "base_url": api.get("base_url"), "api_key_env": api.get("api_key_env") or "SNAPSCRIBE_API_KEY",
        "user_agent": api.get("user_agent") or "snaplab/0.1", "timeout_s": api.get("timeout_s", 0),
        "key_expires": api.get("key_expires"), "slots": "shared", "label": "Snapscribe" if qwen else "Default",
        **({"kind": "snapscribe"} if qwen else {})})
    models.setdefault(model_name, {
        "provider": provider_name, "model": api.get("model") or "configure-model",
        "context_limit": api.get("context_limit") or 131072,
        "effort_style": api.get("effort_style") or ("chat_template_kwargs" if qwen else "none"),
        "label": SNAPSCRIBE_LABEL if qwen else (api.get("model") or "Configure a model")})
    routing = cfg.setdefault("routing", {}) or {}
    routing.setdefault("default", model_name)
    for k in KINDS:
        routing.setdefault(k, routing["default"])
    cfg["providers"], cfg["models"], cfg["routing"] = providers, models, routing


class ConfigError(ValueError):
    pass


def check(cfg: dict) -> list[str]:
    """Problems in the providers/models/routing settings (empty list = fine)."""
    out = []
    for name, m in cfg["models"].items():
        cli = (cfg["providers"].get(m.get("provider")) or {}).get("kind") in CLI_KINDS
        if m.get("provider") not in cfg["providers"]:
            out.append(f"model {name!r} uses an unknown provider {m.get('provider')!r}")
        if not m.get("model") and not cli:            # a CLI may use its own default model
            out.append(f"model {name!r} has no model id")
        if m.get("effort_style", "none") not in EFFORT_STYLES:
            out.append(f"model {name!r}: effort_style must be one of {EFFORT_STYLES}")
    for name, p in cfg["providers"].items():
        if p.get("kind") in CLI_KINDS:
            continue                  # runs a program on this PC (Claude Code, Codex): no URL and no key variable
        if not str(p.get("base_url") or "").startswith(("http://", "https://")):
            out.append(f"provider {name!r} needs a base_url")
        if not p.get("api_key_env"):
            out.append(f"provider {name!r} needs api_key_env (the name of an environment variable)")
    for kind, ref in cfg["routing"].items():
        if ref not in cfg["models"]:
            out.append(f"routing {kind!r} points to an unknown model {ref!r}")
    return out


class ModelRegistry:
    def __init__(self, cfg: dict, shared_slots: Slots, redactor=None, usage=None, override=None):
        self.cfg = cfg
        problems = check(cfg)
        if problems:
            raise ConfigError("; ".join(problems))
        self.redactor = redactor
        self.usage = usage
        self.override = override          # tests: one fake client for every model
        self._slots: dict[str, Slots] = {}
        self._clients: dict[str, LLM] = {}
        for pname, p in cfg["providers"].items():
            s = p.get("slots", "shared" if pname == "snapscribe" else {"count": 2})
            if (s == "shared" or s is None) and pname != "snapscribe" and p.get("kind") != "snapscribe":
                s = {"count": 2}
            if s == "shared" or s is None:
                self._slots[pname] = shared_slots
            else:
                folder = Path(s.get("dir") or Path(cfg["data_dir"]) / "locks" / pname)
                self._slots[pname] = Slots(folder, int(s.get("count") or 4), s.get("session_max"))

    # ------------------------------------------------------------ lookup

    def name_for(self, ref: str | None = None, kind: str = "chat") -> str:
        if ref:
            if ref not in self.cfg["models"]:
                raise ConfigError("The selected model is no longer configured. Choose a model in Settings.")
            return ref
        routing = self.cfg["routing"]
        return routing.get(kind) or routing["default"]

    def spec(self, ref: str | None = None, kind: str = "chat") -> dict:
        name = self.name_for(ref, kind)
        m = self.cfg["models"][name]
        p = self.cfg["providers"][m["provider"]]
        return {"name": name, "provider": m["provider"], "model": m["model"],
                "base_url": str(p.get("base_url") or "").rstrip("/"), "kind": p.get("kind"),
                "api_key_env": p.get("api_key_env") or "", "user_agent": p.get("user_agent") or "snaplab/0.1",
                "timeout_s": p.get("timeout_s", 0), "context_limit": int(m.get("context_limit") or 131072),
                "effort_style": m.get("effort_style", "none"), "extra": {**(p.get("extra") or {}),
                                                                                        **(m.get("extra") or {})},
                "label": m.get("label") or name, "key_expires": p.get("key_expires"),
                "capabilities": m.get("capabilities") or {}}

    def get(self, ref: str | None = None, kind: str = "chat"):
        if self.override is not None:
            return self.override
        name = self.name_for(ref, kind)
        pkind = self.cfg["providers"][self.cfg["models"][name]["provider"]].get("kind")
        if name not in self._clients and pkind in CLI_KINDS:
            from .claude_cli import ClaudeCLI, ToolBridge
            from .codex_cli import CodexCLI
            if getattr(self, "_tool_bridge", None) is None:
                self._tool_bridge = ToolBridge()
            sp = self.spec(name)
            self._clients[name] = (ClaudeCLI if pkind == CLAUDE_CLI else CodexCLI)(
                sp, self.cfg["providers"][sp["provider"]], self.cfg["data_dir"], self._tool_bridge,
                usage=self.usage, redactor=self.redactor)
        if name not in self._clients:
            sp = self.spec(name)
            api_cfg = {"base_url": sp["base_url"], "model": sp["model"], "api_key_env": sp["api_key_env"],
                       "timeout_s": sp["timeout_s"], "user_agent": sp["user_agent"], "effort_style": sp["effort_style"],
                       "extra": sp["extra"], "name": name, "context_limit": sp["context_limit"], "capabilities": sp["capabilities"]}
            self._clients[name] = LLM(api_cfg, self._slots[sp["provider"]], self.redactor, self.usage)
            from .qwen_log import QwenLog
            self._clients[name].qwen_log = QwenLog(self.cfg["data_dir"], self._clients[name].guard,
                (self.cfg.get("qwen_log") or {}).get("enabled", True))
        return self._clients[name]

    def is_cli(self, ref: str | None = None, kind: str = "chat") -> bool:
        return self.spec(ref, kind).get("kind") in CLI_KINDS

    def slots_for(self, ref: str | None = None, kind: str = "chat") -> Slots:
        return self._slots[self.spec(ref, kind)["provider"]]

    def key(self, ref: str | None = None, kind: str = "chat") -> str | None:
        if self.override is not None:
            return self.override.key() if hasattr(self.override, "key") else None
        if self.is_cli(ref, kind):
            return None
        return get_api_key(self.spec(ref, kind)["api_key_env"])

    def has_key(self, ref: str | None = None, kind: str = "chat") -> bool:
        if self.override is not None:
            return bool(self.override.has_key())
        if self.is_cli(ref, kind):
            return self.get(ref, kind).has_key()
        return bool(get_api_key(self.spec(ref, kind)["api_key_env"]))

    def has_any_key(self) -> bool:
        return any(self.has_key(name) for name in self.cfg["models"])

    def key_envs(self) -> list[str]:
        return sorted({p["api_key_env"] for p in self.cfg["providers"].values() if p.get("api_key_env")})

    def find(self, model_ref: str | None) -> str:
        """Model registry name for a name or a provider model id (used by the Codex translator)."""
        if model_ref in self.cfg["models"]:
            return model_ref
        matches = [name for name, m in self.cfg["models"].items() if m.get("model") == model_ref]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ConfigError("Choose the configured model name to identify its provider.")
        if model_ref:
            raise ConfigError("The selected model is no longer configured.")
        return self.name_for(None, "codex")

    def view(self) -> list[dict]:
        """What Settings shows: no key values, only whether each key variable is set. Models in use come first."""
        out = []
        for name in self.cfg["models"]:
            sp = self.spec(name)
            out.append({"name": name, "label": sp["label"], "provider": sp["provider"], "model": sp["model"],
                        "provider_label": self.cfg["providers"][sp["provider"]].get("label") or sp["provider"],
                        "kind": self.cfg["providers"][sp["provider"]].get("kind"),
                        "base_url": sp["base_url"], "capabilities": sp["capabilities"],
                        "context_limit": sp["context_limit"], "key_set": self.has_key(name) if self.override is None else
                        bool(get_api_key(sp["api_key_env"])), "cli": sp.get("kind") in CLI_KINDS,
                        "default_for": [k for k, v in self.cfg["routing"].items() if v == name],
                        "removable": name not in (self.cfg.get("config_models") or ())})
        out.sort(key=lambda m: 0 if m["default_for"] else 1)
        return out
