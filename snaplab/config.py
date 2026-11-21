"""Settings: built-in defaults, overlaid by config.json (git-ignored), overlaid by a few environment variables.

Nothing personal lives in the code. Machine-specific values (paths, tailnet name, lock folder) come from
config.json or are detected at run time (for example the public URL from `tailscale status`).
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent      # the repository folder
WEB = ROOT / "web"

def limit(value) -> float:
    """A configured cap: 0, empty or missing means no limit (math.inf). SnapLab is for long-running work, so caps
    that end work are off by default; only guards against hangs and machine damage keep a value."""
    try:
        value = float(value or 0)
    except (TypeError, ValueError):
        return float("inf")
    return value if value > 0 else float("inf")


DEFAULTS: dict = {
    "listen_host": "127.0.0.1",
    "listen_port": 18445,
    "tailscale_https_port": 8445,
    "public_url": None,              # detected from tailscale when empty
    "data_dir": "data",              # relative to the repository folder, or absolute
    "projects_dir": None,            # default: <data_dir>/projects
    "api": {
        "base_url": "https://api.example.com/v1",
        "model": "configure-model",
        "api_key_env": "SNAPLAB_API_KEY",
        "context_limit": 131072,
        "key_expires": None,         # "YYYY-MM-DD" if your key has an end date
        "chat_effort": "low",        # talking: low (the first call of a turn)
        "after_tools_effort": "medium",   # once a turn has called a tool, the next calls think more (measured: low
                                          # kept working after the task was done in 3 of 5 tasks, medium in 0)
        "desktop_effort": "low",
        "work_max_tokens": 20000,
        "work_effort": "medium",     # sessions and research: medium
        "timeout_s": 600,
        # The provider's Cloudflare front blocks the default Python user agent, so we name ourselves.
        "user_agent": "snaplab/0.1",
    },
    "slots": {
        "dir": None,                 # default: <data_dir>/locks. Point it at a shared folder to share the limit with other tools.
        "count": 2,
        "session_max": 2,            # session work (Codex, bot-only) may hold at most this many, so chat is never starved
        "background_max": 1,         # room summaries and the memory tidy: at most this many (they always wait last)
    },
    "qwen_log": {"enabled": True},
    "proxy": {"port": 18432},
    "search": {"provider": "auto", "cooldown_minutes": 30},
    "push": {"subject": "mailto:snaplab@example.com"},
    # Caps that end long work are 0 = no limit (see limit()): tool steps per bot turn, model calls and minutes per
    # SnapCode turn, continuations a bot makes on its own. Hang guards (idle, single request, tests) keep values.
    "chat": {"history_messages": 24, "max_steps": 0, "max_tokens": 8192},
    "sessions": {"max_steps": 0, "max_api_calls": 0, "codex_timeout_min": 0, "test_timeout_s": 180,
                 "idle_timeout_s": 600, "auto_continue": 0},
    "require_tailscale_user": True,
    "local_mode": False,            # explicitly enabled by the first-run launcher; legacy installs keep their gate
    "owner_login": None,             # detected from tailscale when empty
    "tailscale_exe": None,
    "manage_tailscale_serve": True,
    "harness_plugins": [],
    "tool_dirs": None,               # folders with tool plugins; None = plugins/tools and <data>/plugins/tools
    "mcp_servers": {},               # external MCP servers whose tools bots may use (see snaplab/mcp_client.py)
    "task_name": "SnapLab",
    # Providers and models (see snaplab/models.py and docs/extending.md). Empty = derived from "api" above.
    "providers": {},
    "models": {},
    "routing": {},
    "sse_seconds": 900,
    "memory_chars": 4000,
    "user_note_chars": 1400,         # data/shared/user.md: what every bot knows about the user
    # Background upkeep that calls the model (see snaplab/background.py); it always waits behind chats.
    "summary": {"enabled": True, "every_messages": 20, "recent_tokens": 6000, "max_tokens": 1000, "min_recent": 6,
                "effort": "low", "input_tokens": 9000},
    "memory_tidy": {"enabled": True, "hour": 3, "every_days": 1, "min_chars": 1200, "effort": "low"},            # each bot's long-term memory file; bigger writes are refused (the bot must tidy up)
    # What a bot sees per call (context.py).
    # Measured with the Snapscribe model (2026-11-01): first token after ~2.6 s at 16K, ~6 s at 32K, ~12 s at 64K for a new
    # context; quality held to 64K. Chats and rooms stop at 32K; background sessions may use up to 64K.
    "context": {"target_tokens": 16000, "summarize_tokens": 24000, "max_tokens": 32000, "session_max_tokens": 64000,
                # memory_tokens, user_tokens, summary_tokens, brief_tokens and recent_tokens are sized from each
                # model's window when not set here (context.budgets).
                "message_tokens": 900, "history_messages": 40},
    "context_builder": None,         # "module:Class" to replace the default context builder
    # Proposal cards: which bot actions wait for the user's approval ("always" | "after_web" | "never")
    "approvals": {},                 # Explicit overrides only; backend-aware defaults live in tools.py.
    "max_bots": None,                # no limit; set a number to cap how many bots may exist
    "rooms": {"default_responder": "all"},  # all members can choose to speak or stay silent
    "bot_conversations": {"max_consecutive": 6, "max_hourly": 0, "repeat_window": 20},   # max_hourly 0 = no limit
    "codex": {"read_paths": [], "backend": "local",
              "remote": {"host": None, "user": "snaplab", "ssh_exe": "ssh", "ssh_port": 22,
                         "codex_bin": ".local/bin/codex"}},
    # Exit guard: besides the API key, environment variables whose values must never leave SnapLab
    "guard": {"secret_env": []},     # extra folders the Codex sandbox may read (the Python install is added automatically)
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def config_path() -> Path:
    env = os.environ.get("SNAPLAB_CONFIG")
    return Path(env) if env else ROOT / "config.json"


def load(path: Path | None = None, overrides: dict | None = None) -> dict:
    """Return the effective settings. SNAPLAB_DATA overrides data_dir (handy for tests and trial runs)."""
    p = Path(path) if path else config_path()
    data = {}
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8"))
    defaults = DEFAULTS
    # Existing pre-installer configurations may omit the old api fields. Preserve their
    # previous effective values; new/no-config and explicit local installs use neutral defaults.
    if p.exists() and not data.get("local_mode") and not (overrides or {}).get("local_mode"):
        from .presets import snapscribe
        preset = snapscribe(data)
        defaults = _merge(DEFAULTS, {"api": {"base_url": preset["base_url"],
            "model": preset["model"], "api_key_env": "SNAPSCRIBE_API_KEY"}, "slots": {"count": 3}})
    cfg = _merge(defaults, data)
    cfg = _merge(cfg, overrides or {})
    cfg["_legacy_api_names"] = defaults is not DEFAULTS
    if os.environ.get("SNAPLAB_DATA"):
        cfg["data_dir"] = os.environ["SNAPLAB_DATA"]
    if os.environ.get("SNAPLAB_PORT"):
        cfg["listen_port"] = int(os.environ["SNAPLAB_PORT"])
    data_dir = Path(cfg["data_dir"])
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    cfg["data_dir"] = str(data_dir.resolve())
    proj = cfg.get("projects_dir")
    cfg["projects_dir"] = str(Path(proj).resolve()) if proj else str(data_dir.resolve() / "projects")
    slots_dir = cfg["slots"].get("dir")
    cfg["slots"]["dir"] = str(Path(slots_dir).resolve()) if slots_dir else str(data_dir.resolve() / "locks")
    cfg["slots"]["count"] = max(1, int(cfg["slots"].get("count") or 2))
    provider_file = data_dir.resolve() / "provider.json"
    added: set = set()
    if provider_file.exists():
        overlay = json.loads(provider_file.read_text(encoding="utf-8"))
        added = set((overlay.get("models") or {}) if isinstance(overlay, dict) else {}) - set(cfg.get("models") or {})
        cfg = _merge(cfg, overlay)
    if cfg.get("local_mode"):
        cfg["public_url"] = f"http://127.0.0.1:{cfg['listen_port']}"
        cfg["require_tailscale_user"] = False
        cfg["manage_tailscale_serve"] = False
        cfg["proxy"]["port"] = 0   # choose a free translator port for this local instance
    from .models import derive
    derive(cfg)
    # Models from config.json and the defaults (Settings can remove only the ones added there, in provider.json).
    cfg["config_models"] = sorted(set(cfg.get("models") or {}) - added)
    return cfg
