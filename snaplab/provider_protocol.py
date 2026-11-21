"""Small Chat Completions capability table shared by Bot and the Codex translator."""
from __future__ import annotations

import copy
import json
import threading
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class RequestCancelled(Exception):
    """open_request(cancel=...) gave up because the caller was stopped."""


def open_request(req, *, timeout=None, cancel=None):
    """Open a request without following redirects (neither credentials nor conversation bodies may follow one).

    timeout None waits without a time limit. With cancel (a function), the wait ends as soon as cancel() is true,
    also while the caller still reads the answer: the connection is closed and RequestCancelled is raised (a read
    that was cut raises the usual OSError or ValueError)."""
    opener = urllib.request.build_opener(NoRedirect())
    if cancel is None:
        return opener.open(req, timeout=timeout)
    if cancel():
        raise RequestCancelled()
    box, done, lock = {}, threading.Event(), threading.Lock()

    def run():
        try:
            box["resp"] = opener.open(req, timeout=timeout)
        except BaseException as e:  # noqa: BLE001 - handed to the caller
            box["error"] = e
        with lock:
            done.set()
            late = box.get("abandoned") and box.get("resp")
        if late:
            _close(late)
    threading.Thread(target=run, daemon=True, name="open-request").start()
    while not done.wait(0.2):
        if cancel():
            with lock:
                box["abandoned"] = not done.is_set()
            if box["abandoned"]:
                raise RequestCancelled()
            break
    if "error" in box:
        raise box["error"]
    resp = box["resp"]

    def watch():                         # close the answer when the caller is stopped while it reads
        while not getattr(resp, "closed", True):
            if cancel():
                _close(resp)
                return
            threading.Event().wait(0.2)
    threading.Thread(target=watch, daemon=True, name="open-request-watch").start()
    return resp


def _close(resp):
    try:
        resp.close()
    except Exception:  # noqa: BLE001
        pass


def capabilities(spec):
    qwen = spec.get("effort_style") == "chat_template_kwargs"
    return {"tools": True, "vision": True, "stream_usage": True,
            "reasoning_history": qwen, "token_parameter": "max_tokens",
            "max_output_tokens": 32768, "protocol": "chat",
            **(spec.get("capabilities") or {})}


def estimate_input(body):
    from .context import estimate_tokens
    measured = copy.deepcopy(body.get("messages", []))
    image_count = 0
    for message in measured:
        if isinstance(message.get("content"), list):
            for part in message["content"]:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    part["image_url"] = "[image]"
                    image_count += 1
    return estimate_tokens(json.dumps({"messages": measured, "tools": body.get("tools")}, ensure_ascii=False)) + image_count * 2048


def prepare(body, spec):
    """Reject unsupported input before sending; never retry or change providers."""
    from .llm import ApiError
    caps = capabilities(spec)
    if caps["protocol"] != "chat":
        raise ApiError(400, "This model requires an unsupported API protocol.", "protocol")
    out = copy.deepcopy(body)
    if out.get("tools") and not caps["tools"]:
        raise ApiError(400, "This model cannot use tools.", "tools")
    images = any(isinstance(m.get("content"), list) and any(
        p.get("type") == "image_url" for p in m["content"] if isinstance(p, dict))
        for m in out.get("messages", []))
    if images and not caps["vision"]:
        raise ApiError(400, "This model cannot read images.", "vision")
    if not caps["reasoning_history"]:
        for m in out.get("messages", []):
            m.pop("reasoning_content", None)
    if spec.get("effort_style", "none") != "chat_template_kwargs":
        out.pop("chat_template_kwargs", None)
    if not caps["stream_usage"] or not out.get("stream"):
        out.pop("stream_options", None)
    limit = int(spec.get("context_limit") or 131072)
    # Reserve output and a margin. Do not silently discard instructions or tool results.
    requested = out.pop("max_tokens", out.pop("max_completion_tokens", None))
    maximum = max(1, min(int(requested) if requested is not None else int(caps["max_output_tokens"]),
                         int(caps["max_output_tokens"]), limit // 4))
    from .context import estimate_tokens
    estimated = estimate_input(out)
    if estimated + maximum + min(1024, limit // 10) > limit:
        raise ApiError(400, "The request exceeds this model's context limit.", "context")
    parameter = caps["token_parameter"]
    if parameter not in ("max_tokens", "max_completion_tokens", "none"):
        raise ApiError(400, "Invalid output limit setting.")
    # Codex historically leaves the output limit to the provider. Preserve that
    # behavior when no limit was requested instead of imposing a 4096-token cap.
    if parameter != "none" and requested is not None:
        out[parameter] = maximum
    return out


def tool_fragments(delta):
    """Accept missing indexes and object arguments as well as normal streamed strings."""
    for pos, call in enumerate(delta.get("tool_calls") or []):
        fn = call.get("function") or {}
        args = fn.get("arguments") or ""
        if isinstance(args, dict):
            args = json.dumps(args, ensure_ascii=False)
        yield {**call, "index": int(call.get("index", pos) or 0),
               "function": {**fn, "arguments": args}}
