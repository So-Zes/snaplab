"""Session harnesses. Built in: "codex" (sandboxed coding agent), "bot" (talk and research only) and "tests"
(runs the project's tests in the sandbox, no model - the smallest example of the contract).

Extra harnesses can be listed in config.json as "harness_plugins": ["package.module:ClassName"].
Every harness is checked against the contract (contract.py) when it is loaded; one that does not conform,
or that runs code without declaring a sandbox, is not loaded. See docs/extending.md.
"""
from __future__ import annotations

import importlib

from . import contract
from .base import Harness, TurnResult  # noqa: F401
from .bot import BotHarness
from .codex import CodexHarness
from .tests import TestsHarness
from .pi import PiHarness

BUILTIN = [CodexHarness, PiHarness, BotHarness, TestsHarness]


def load(app) -> dict[str, Harness]:
    out: dict[str, Harness] = {}
    candidates = [(cls.__name__, cls) for cls in BUILTIN
                  if cls is not PiHarness or (app.cfg.get("codex") or {}).get("backend") in ("ssh", "wsl")]
    for spec in app.cfg.get("harness_plugins") or []:
        mod, _, name = str(spec).partition(":")
        try:
            candidates.append((spec, getattr(importlib.import_module(mod), name)))
        except Exception as e:  # noqa: BLE001
            app.log(f"could not load harness {spec}: {e!r}")
    for label, cls in candidates:
        try:
            h = cls(app)
        except Exception as e:  # noqa: BLE001
            app.log(f"harness {label} failed to start: {e!r}")
            continue
        bad = contract.problems(h)
        if bad:
            app.log(f"harness {label} does not follow the contract and was not loaded: {'; '.join(bad)}")
            continue
        if h.id in out:
            app.log(f"harness {label}: the id {h.id!r} is already used; not loaded")
            continue
        out[h.id] = h
    return out
