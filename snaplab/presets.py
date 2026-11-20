"""Provider presets offered on the setup page, kept in one place so an address or model can change without a release.

Order of precedence (later wins):
1. BASE below.
2. "presets" in config.json, for example {"presets": {"snapscribe": {"base_url": "https://.../v1"}}}.
3. Environment variables for the Snapscribe preset: SNAPLAB_SNAPSCRIBE_URL and SNAPLAB_SNAPSCRIBE_MODEL
   (the installer can pass these through).

The setup page also lets the user edit the address before testing it, so a changed endpoint never needs code.
"""
from __future__ import annotations

import copy
import os

SNAPSCRIBE_LABEL = "Snapscribe 1"   # what the app calls the Snapscribe model, whichever model the service runs now

BASE = {
    "snapscribe": {"label": SNAPSCRIBE_LABEL, "base_url": "https://gpu.snapscribeapp.com/yami/v1",
                   "model": "qwen3.8-27b", "context_limit": 131072, "vision": True, "fixed_model": True},
    "openrouter": {"label": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "model": "",
                   "context_limit": 32768, "vision": False, "fixed_model": False},
    "opencode": {"label": "OpenCode Zen / Go", "base_url": "https://opencode.ai/zen/v1", "model": "",
                 "context_limit": 32768, "vision": False, "fixed_model": False},
    # Gemini through Google's OpenAI-compatible endpoint, with an AI Studio key (the Gemini CLI sign-in may not be
    # used by other apps, see cli_admin.py).
    "gemini": {"label": "Google Gemini (API key)", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
               "model": "", "context_limit": 1048576, "vision": True, "fixed_model": False},
    "custom": {"label": "Other OpenAI-compatible API", "base_url": "", "model": "",
               "context_limit": 32768, "vision": False, "fixed_model": False},
}
FIELDS = ("label", "base_url", "model", "context_limit", "vision", "fixed_model")


def presets(cfg: dict | None = None) -> dict:
    """Return every preset with overrides applied. Unknown preset names and fields in config are ignored."""
    out = copy.deepcopy(BASE)
    for name, override in ((cfg or {}).get("presets") or {}).items():
        if name in out and isinstance(override, dict):
            out[name].update({k: v for k, v in override.items() if k in FIELDS})
    env = {"base_url": os.environ.get("SNAPLAB_SNAPSCRIBE_URL"), "model": os.environ.get("SNAPLAB_SNAPSCRIBE_MODEL")}
    out["snapscribe"].update({k: v.strip() for k, v in env.items() if v and v.strip()})
    return out


def snapscribe(cfg: dict | None = None) -> dict:
    return presets(cfg)["snapscribe"]
