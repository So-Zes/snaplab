"""Small Chat Completions capability table shared by Bot and the Codex translator."""
from __future__ import annotations

import copy
import json
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def open_request(req, **kwargs):
    # Neither credentials nor conversation bodies may follow a redirect.
    return urllib.request.build_opener(NoRedirect()).open(req, **kwargs)


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
