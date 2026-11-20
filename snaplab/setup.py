"""First-run provider setup. Called only by a paired device on the local origin."""
from __future__ import annotations

import hashlib
import json
import secrets
import copy
import shutil
import time
import threading
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from . import credentials
from .models import ModelRegistry, derive
from .presets import presets
from .provider_protocol import prepare, open_request
from .slots import Slots

_LOCK = threading.Lock()


class SetupError(ValueError):
    def __init__(self, code):
        super().__init__("Provider connection failed. Check the key, account and selected model.")
        self.error_code = code


def needed(app):
    return bool(app.cfg.get("local_mode") and not app.store.path("provider.json").exists()
                and not app.store.settings().get("onboarded"))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_fields(body):
    url = str(body.get("base_url") or "").strip().rstrip("/")
    model = str(body.get("model") or "").strip()
    key = str(body.get("key") or "").strip()
    try:
        u = urlsplit(url)
        port = u.port
    except ValueError:
        raise ValueError("Enter a valid API base URL.") from None
    if (u.scheme not in ("https", "http") or not u.hostname or u.username or u.password or u.query or u.fragment
            or any(ch.isspace() or ord(ch) < 32 for ch in url) or len(url) > 2048):
        raise ValueError("Enter an API base URL without a password, query or fragment.")
    if u.scheme == "http" and u.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("Use HTTPS for a remote provider. HTTP is allowed only on this PC.")
    if not model or len(model) > 200 or any(ord(ch) < 32 for ch in model):
        raise ValueError("Enter the model name supplied by your provider.")
    if not key or len(key.encode("utf-16-le")) > 2560 or any(ord(ch) < 32 for ch in key):
        raise ValueError("Enter a valid API key.")
    # Credentials must never be persisted in the supposedly non-secret fields.
    if key in url or key in model:
        raise ValueError("Put the API key only in the API key field.")
    style = "chat_template_kwargs" if "qwen" in model.lower() else "none"
    return url, model, key, style


def check_connection(url, model, key, style, slots, spec=None):
    body = {"model": model, "messages": [{"role": "user", "content": "Reply with OK."}],
            "max_tokens": 16, "stream": False}
    if style == "chat_template_kwargs":
        body["chat_template_kwargs"] = {"enable_thinking": False}
    body = prepare(body, spec or {"effort_style": style})
    req = urllib.request.Request(url + "/chat/completions", data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json", "User-Agent": "snaplab/0.1"})
    try:
        # No redirects: the Authorization header must never follow a provider to another host.
        with slots.acquire(kind="chat"), urllib.request.build_opener(NoRedirect()).open(req, timeout=30) as response:
            result = json.loads(response.read(65537))
        choices = result.get("choices") or []
        if not choices or not isinstance(choices[0].get("message"), dict):
            raise ValueError("shape")
    except urllib.error.HTTPError as exc:
        # Do not read or echo the response body; a server can echo the key.
        code = exc.code
        exc.close()
        from .llm import ApiError, error_code
        raise SetupError(error_code(ApiError(code, ""))) from None
    except Exception:
        raise ValueError("The API test did not complete. Check the URL and connection, then try again.") from None


def configure(app, body, *, add=False):
    with _LOCK:
        if not add and not needed(app):
            raise ValueError("Provider setup is already complete.")
        url, model, key, style = validate_fields(body)
        app.guard.add(key)
        preset = str(body.get("provider") or "custom")
        choices = presets(app.cfg)
        if preset not in choices:
            raise ValueError("Choose a supported provider.")
        # The same model hosted by another provider does not imply vLLM-specific fields.
        if preset != "snapscribe" and "provider" in body:
            style = "none"
        caps = submitted_capabilities(body, preset, model)
        spec = {"effort_style": style, "capabilities": caps, "context_limit": context_limit(body)}
        # Each registration owns its key and endpoint. It never updates another provider's URL.
        suffix = secrets.token_hex(4)
        pname = preset + "-" + suffix
        test_slots = app.slots if preset == "snapscribe" else Slots(Path(app.cfg["data_dir"]) / "locks" / pname, 2)
        if "provider" in body:
            check_connection(url, model, key, style, test_slots, spec)
        else:
            check_connection(url, model, key, style, test_slots)
        # A per-installation target cannot overwrite a pre-existing provider credential.
        target = "SNAPLAB_" + hashlib.sha256(str(Path(app.cfg["data_dir"]).resolve()).encode()).hexdigest()[:16] + "_" + secrets.token_hex(4)
        credentials.write_credential(target, key)
        api = {**app.cfg["api"], "base_url": url, "model": model, "api_key_env": target, "effort_style": style,
               "context_limit": spec["context_limit"]}
        if add or "provider" in body:
            overlay = {"providers": copy.deepcopy(app.cfg["providers"]) if add else {},
                       "models": copy.deepcopy(app.cfg["models"]) if add else {},
                       "routing": copy.deepcopy(app.cfg["routing"]) if add else {}}
            overlay["providers"][pname] = {"base_url": url, "api_key_env": target, "kind": preset,
                "label": "Snapscribe" if preset == "snapscribe" else choices[preset]["label"],
                "slots": "shared" if preset == "snapscribe" else {"count": 2}}
            overlay["models"][pname] = {"provider": pname, "model": model,
                                       "label": choices[preset]["label"] if preset == "snapscribe" else model + " (" + choices[preset]["label"] + ")",
                                       **spec}
            if not add:
                overlay["api"] = api
                overlay["routing"] = {k: pname for k in ("default", "chat", "session", "codex", "background")}
            elif app.store.path("provider.json").exists():
                previous = app.store.read_json("provider.json", {})
                if "api" in previous:
                    overlay["api"] = previous["api"]
        else:
            overlay = {"api": api, "providers": {}, "models": {}, "routing": {}}
            derive(overlay)
        candidate = {**app.cfg, **overlay}
        registry = ModelRegistry(candidate, app.slots, app.redactor, app.usage)
        # Only allowlisted metadata is written. The submitted body is never serialized.
        saved = app.store.path("provider.json")
        if saved.exists():
            shutil.copy2(saved, saved.with_name("provider.json.bak-" + str(time.time_ns())))
        app.store.write_json("provider.json", overlay)
        app.cfg.update(overlay)
        registry.cfg = app.cfg
        app.models = registry
        app.guard.extra_env = sorted(set(app.guard.extra_env) | set(app.models.key_envs()))
        # Recreate the translator with the new registry (and an automatically chosen local port).
        if app.translator and not add:
            app.translator.stop()
            app.translator = None
            app.start_translator()
        return {"configured": True}


def _persist(app, providers, models, routing):
    """Check and save a changed model list in provider.json, then use it at once. The "api" block is kept."""
    candidate = {**app.cfg, "providers": providers, "models": models, "routing": routing}
    registry = ModelRegistry(candidate, app.slots, app.redactor, app.usage)
    saved = app.store.path("provider.json")
    previous = app.store.read_json("provider.json", {}) if saved.exists() else {}
    if saved.exists():
        shutil.copy2(saved, saved.with_name("provider.json.bak-" + str(time.time_ns())))
    overlay = {"providers": providers, "models": models, "routing": routing}
    if "api" in previous:
        overlay["api"] = previous["api"]
    app.store.write_json("provider.json", overlay)
    app.cfg.update(overlay)
    registry.cfg = app.cfg
    app.models = registry
    return {"models": registry.view(), "routing": app.cfg["routing"]}


def set_default(app, name):
    """Make a model the one bots use when they have no model of their own (routing "chat")."""
    with _LOCK:
        if name not in app.cfg["models"]:
            raise LookupError("no such model")
        routing = {**app.cfg["routing"], "chat": name}
        return _persist(app, copy.deepcopy(app.cfg["providers"]), copy.deepcopy(app.cfg["models"]), routing)


def remove_model(app, name):
    """Remove a model added in Settings. Bots that used it go back to the default; an unused key is deleted."""
    with _LOCK:
        models = copy.deepcopy(app.cfg["models"])
        if name not in models:
            raise LookupError("no such model")
        uses = sorted(k for k, v in app.cfg["routing"].items() if v == name)
        if uses:
            raise ValueError("This model is a default (" + ", ".join(uses) + "). Make another model the default first.")
        added = (app.store.read_json("provider.json", {}) or {}).get("models") or {}
        if name not in added:
            raise ValueError("This model comes from config.json. Remove it there.")
        pname = models.pop(name)["provider"]
        providers = copy.deepcopy(app.cfg["providers"])
        dropped_env = None
        if not any(m.get("provider") == pname for m in models.values()):
            dropped = providers.pop(pname, None) or {}
            env = dropped.get("api_key_env") or ""
            still_used = any(p.get("api_key_env") == env for p in providers.values()) or \
                (app.cfg.get("api") or {}).get("api_key_env") == env
            if env.startswith("SNAPLAB_") and not still_used:
                dropped_env = env
        result = _persist(app, providers, models, copy.deepcopy(app.cfg["routing"]))
        for bot in app.store.bots():
            if bot.get("model") == name:
                bot["model"] = None
                app.store.save_bot(bot)
        if dropped_env:
            try:
                credentials.delete_credential(dropped_env)
            except Exception:
                pass
        return result


def context_limit(body):
    try:
        value = int(body.get("context_limit") or 32768)
    except (TypeError, ValueError):
        raise ValueError("Enter a context limit between 2048 and 2000000.") from None
    if not 2048 <= value <= 2000000:
        raise ValueError("Enter a context limit between 2048 and 2000000.")
    return value


def opencode_protocol(model, base_url=""):
    # Zen's GPT and Claude routes are not Chat Completions. Unknown families are
    # conservatively unavailable until their Chat Completions support is known.
    m = model.lower().split("/")[-1]
    if m.startswith(("gpt-", "o1", "o3", "o4", "grok-", "muse-")):
        return "responses"
    if m.startswith(("claude", "qwen")) or ("/go/" in base_url and m.startswith("minimax-")):
        return "messages"
    if m.startswith(("glm-", "kimi-", "minimax-", "big-pickle", "deepseek-", "mimo-", "longcat-", "hy3", "hy4", "space-bunny")):
        return "chat"
    return "unsupported"


def submitted_capabilities(body, preset, model):
    parameter = body.get("token_parameter") or "max_tokens"
    if parameter not in ("max_tokens", "max_completion_tokens", "none"):
        raise ValueError("Choose a supported output limit setting.")
    protocol = opencode_protocol(model, str(body.get("base_url") or "")) if preset == "opencode" else "chat"
    if protocol != "chat":
        raise ValueError("This OpenCode model requires Responses or Messages and is not supported here.")
    return {"protocol": protocol, "tools": body.get("tools", True) is True,
            "vision": body.get("vision", preset == "snapscribe") is True,
            "stream_usage": body.get("stream_usage", True) is True,
            "token_parameter": parameter, "reasoning_history": preset == "snapscribe" and "qwen" in model.lower()}


def discover(app, body):
    # The same URL checks apply to both catalog and probe; never reuse a saved key
    # with a URL supplied by the browser.
    url, _, key, _ = validate_fields({**body, "model": "catalog"})
    app.guard.add(key)
    req = urllib.request.Request(url + "/models", headers={"User-Agent": "snaplab/0.1", "Accept": "application/json"})
    req.add_unredirected_header("Authorization", "Bearer " + key)
    try:
        with open_request(req, timeout=20) as response:
            result = json.loads(response.read(2_000_001))
        rows = []
        for m in (result.get("data") or [])[:2000]:
            mid = m.get("id")
            if body.get("provider") == "gemini" and isinstance(mid, str) and mid.startswith("models/"):
                mid = mid[len("models/"):]          # Gemini lists "models/<id>"; chat takes the bare id
            if not isinstance(mid, str) or not mid or len(mid) > 200 or key in mid:
                continue
            protocol = opencode_protocol(mid, url) if body.get("provider") == "opencode" else "chat"
            params = m.get("supported_parameters")
            architecture = m.get("architecture") or {}
            rows.append({"id": mid, "supported": protocol == "chat", "protocol": protocol,
                         "context_limit": m.get("context_length") or 32768,
                         "tools": "tools" in params if isinstance(params, list) else True,
                         "vision": "image" in architecture.get("input_modalities", []),
                         "token_parameter": "max_tokens"})
        return {"models": app.guard.clean_obj(rows)}
    except Exception:
        raise ValueError("Model list is unavailable. Enter a Chat Completions model ID manually.") from None
