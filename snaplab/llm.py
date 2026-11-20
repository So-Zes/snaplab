"""Client for OpenAI-compatible Chat Completions APIs (Snapscribe by default; see presets.py).

Facts about this server that the client relies on (measured on the real endpoint):
- Only /chat/completions exists. The context limit is 131,072 tokens for input and output together.
- Thinking depth works only as chat_template_kwargs.reasoning_effort with low / medium / xhigh
  (a top-level reasoning_effort is silently dropped, "high" is rejected). The server default is xhigh.
  SnapLab uses low for talking and medium for work.
- Earlier thinking can be sent back as assistant `reasoning_content` inside one task.
- The default Python user agent is blocked by the provider's Cloudflare front, so we send our own name.

Every request holds one concurrency slot (slots.py) for its whole duration. Nothing is retried
automatically: the GPU is shared with the provider's team. The API key is read from the environment,
Windows Credential Manager, or the legacy registry environment; it is never written by this client.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from .provider_protocol import open_request, prepare, tool_fragments

EFFORTS = {"minimal": "low", "low": "low", "medium": "medium", "high": "xhigh", "xhigh": "xhigh"}
THINK_RE = re.compile(r"<think>(.*?)</think>", re.S)


def get_api_key(env_name: str) -> str | None:
    """Process environment, Windows generic credential, then legacy HKCU environment."""
    v = (os.environ.get(env_name) or "").strip()
    if v:
        return v
    return credential_api_key(env_name) or registry_api_key(env_name)


def credential_api_key(env_name: str) -> str | None:
    """Skip non-Windows hosts and tolerate inaccessible credentials without exposing errors."""
    if os.name == "nt":
        from .credentials import read_generic_credential
        try:
            return read_generic_credential(env_name)
        except OSError:
            pass
    if os.name != "nt":
        from .credentials import read_system_credential
        return read_system_credential(env_name)
    return None


def registry_api_key(env_name: str) -> str | None:
    """Read only the Windows per-user environment; never log the value."""
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as h:
                value, _ = winreg.QueryValueEx(h, env_name)
                if value:
                    return str(value).strip()
        except OSError:
            pass
    return None


def api_key_values(env_name: str) -> set[str]:
    """All current values to guard, independent of the key selected for authentication."""
    return {v.strip() for v in (os.environ.get(env_name), credential_api_key(env_name), registry_api_key(env_name))
            if v and v.strip()}


class ApiError(Exception):
    def __init__(self, status: int | None, message: str, hint: str = ""):
        super().__init__(message)
        self.status = status
        self.message = message
        self.hint = hint

    def short(self) -> str:
        return f"{self.message[:300]}" + (f" ({self.hint})" if self.hint else "")


def explain(status: int | None, message: str) -> str:
    m = (message or "").lower()
    if status is None:
        if "timed out" in m or "timeout" in m:
            return "timed out; the GPU may be busy"
        return "cannot reach the API server"
    if status == 401:
        return "the API key was not accepted (wrong value or expired)"
    if status == 403:
        if "1010" in m or "cloudflare" in m:
            return "the provider's Cloudflare front rejected the client name"
        return "the key has no permission for this model"
    if status == 402:
        return "the provider balance or quota is exhausted"
    if status == 410:
        return "this model is no longer available"
    if status in (400, 422) and "tool" in m:
        return "this model does not support tools"
    if status == 404:
        return "wrong base URL or model name"
    if status in (400, 413, 422):
        if "context" in m or "too long" in m or "maximum" in m or status == 413:
            return "the request is longer than the model's context limit"
        return "the server did not accept the request format"
    if status == 429:
        return "rate limited or busy; try again in a few minutes"
    if 500 <= status < 600:
        return "server-side problem (shared GPU busy or down)"
    return "unexpected response"


@dataclass
class Reply:
    content: str = ""
    reasoning: str = ""
    tool_calls: list = field(default_factory=list)      # [{"id", "name", "arguments"(str)}]
    finish_reason: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int | None = None
    elapsed_s: float = 0.0
    finished: bool = True          # False when the stream ended without [DONE] / finish_reason


class Usage:
    """Counts calls and tokens. The store persists the totals; this object just forwards."""

    def __init__(self, on_record=None):
        self.on_record = on_record

    def record(self, *, input_tokens: int, output_tokens: int, ok: bool, source: str, model: str | None = None,
               caller: str | None = None) -> None:
        if self.on_record:
            try:
                if caller is None:          # the room or session whose turn made this call (set by the agent loop)
                    from .qwen_log import caller_id
                    caller = caller_id.get()
                self.on_record(input_tokens=input_tokens, output_tokens=output_tokens, ok=ok, source=source, model=model,
                               caller=caller)
            except Exception:  # noqa: BLE001 - accounting must never break a reply
                pass


def effort_kwargs(effort: str | None) -> dict | None:
    if not effort or effort in ("server", "default"):
        return None
    e = str(effort).lower()
    if e in ("off", "none"):
        return {"enable_thinking": False}
    if e not in EFFORTS:
        raise ValueError(f"unknown reasoning effort: {effort}")
    return {"reasoning_effort": EFFORTS[e]}


def apply_effort(body: dict, effort: str | None, style: str) -> None:
    """Put the thinking depth into the request the way this model understands it."""
    kw = effort_kwargs(effort)
    if not kw or style == "none":
        return
    if style == "reasoning_effort":
        if "reasoning_effort" in kw:
            body["reasoning_effort"] = {"xhigh": "high"}.get(kw["reasoning_effort"], kw["reasoning_effort"])
        return
    body["chat_template_kwargs"] = {**(body.get("chat_template_kwargs") or {}), **kw}


class Cancelled(Exception):
    pass


def error_code(e: Exception) -> str:
    """A short, stable code for an error, used by the phone to show a plain sentence in the user's language (QA B-3).
    cut | nokey | key | denied | config | too_long | rejected | busy | server | timeout | unreachable | internal"""
    if isinstance(e, StreamCut):
        return "cut"
    if not isinstance(e, ApiError):
        return "internal"
    s, m = e.status, (e.message or "").lower() + " " + (e.hint or "").lower()
    if s is None:
        if "is not set" in m:
            return "nokey"
        return "timeout" if ("timed out" in m or "timeout" in m) else "unreachable"
    if s == 401:
        return "key"
    if s == 403:
        return "denied"
    if s in (404, 410):
        return "retired"
    if s == 402 or any(x in m for x in ("insufficient", "credit", "quota", "balance")):
        return "credit"
    if s in (400, 422) and "tool" in m:
        return "tools"
    if s in (400, 413, 422):
        return "too_long" if ("context" in m or "too long" in m or s == 413) else ("vision" if "vision" in m else "rejected")
    if s == 429:
        return "busy"
    if 500 <= s < 600:
        return "server"
    return "rejected"


def stream_error(value):
    """Provider stream errors have the same safe classification as HTTP errors."""
    error = value if isinstance(value, dict) else {}
    try:
        status = int(error.get("code") or error.get("status") or 502)
    except (TypeError, ValueError):
        status = 502
    message = str(error.get("message") or "")
    return ApiError(status, explain(status, message), error_code(ApiError(status, message)))


class StreamCut(ApiError):
    """The answer stream ended without its end marker: what arrived is kept in .partial (QA B-4)."""

    def __init__(self, partial: "Reply"):
        super().__init__(None, "the reply was cut off before it finished", "the connection to the model ended early")
        self.partial = partial


class LLM:
    """Real client. stream_chat() streams thinking and text through callbacks and returns a Reply."""

    is_real = True

    def __init__(self, api_cfg: dict, slots, redactor=None, usage: Usage | None = None):
        self.api_cfg = dict(api_cfg)
        self.base_url = api_cfg["base_url"].rstrip("/")
        self.model = api_cfg["model"]
        self.name = api_cfg.get("name") or self.model
        self.key_env = api_cfg.get("api_key_env") or "SNAPSCRIBE_API_KEY"
        self.timeout = float(api_cfg.get("timeout_s", 600))
        self.user_agent = api_cfg.get("user_agent") or "snaplab/0.1"
        self.effort_style = api_cfg.get("effort_style") or ("chat_template_kwargs" if "qwen" in self.model.lower() else "none")
        self.extra = dict(api_cfg.get("extra") or {})
        self.slots = slots
        self.redactor = redactor
        from .guard import Guard
        self.guard = redactor.guard if redactor else Guard()
        if self.key_env not in self.guard.extra_env:
            self.guard.extra_env.append(self.key_env)
        self.usage = usage or Usage()

    def key(self) -> str | None:
        return get_api_key(self.key_env)

    def has_key(self) -> bool:
        return bool(self.key())

    def _scrub(self, text: str, key: str | None) -> str:
        self.guard.add(key)
        text = self.guard.clean(text)
        if key and text:
            text = text.replace(key, "***")
        return text

    def _record(self, r: "Reply | None", ok: bool, source: str) -> None:
        self.usage.record(input_tokens=r.input_tokens if r else 0, output_tokens=r.output_tokens if r else 0, ok=ok,
                          source=source, model=self.name)

    def stream_chat(self, messages: list[dict], *, tools: list | None = None, effort: str | None = "low",
                    max_tokens: int = 4096, on_reasoning=None, on_content=None, cancel=None,
                    source: str = "chat", kind: str = "chat", on_wait=None, tool_choice: str = "auto") -> Reply:
        from .slots import SlotsBusy
        key = self.key()
        if not key:
            raise ApiError(None, f"{self.key_env} is not set", f"set the environment variable {self.key_env}")
        if self.redactor:
            self.redactor.add_secret(key)
            messages = self.redactor.clean_obj(messages)
        if self.effort_style != "chat_template_kwargs":
            messages = [{k: v for k, v in m.items() if k != "reasoning_content"} for m in messages]
        body: dict = {"model": self.model, "messages": messages, "max_tokens": int(max_tokens), "stream": True,
                      "stream_options": {"include_usage": True}}
        for k, v in self.extra.items():
            body.setdefault(k, v)
        if tools:
            body["tools"] = self.redactor.clean_obj(tools) if self.redactor else tools
            body["tool_choice"] = tool_choice
        apply_effort(body, effort, self.effort_style)
        body = prepare(body, {**self.api_cfg, "effort_style": self.effort_style})
        body = self.guard.clean_obj(body)
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json",
                     "User-Agent": self.user_agent, "Accept": "text/event-stream"})
        req.add_unredirected_header("Authorization", f"Bearer {key}")
        waited = []

        def note_wait(pos):
            waited.append(pos)
            if on_wait:
                _safe(on_wait, pos)
        try:
            slot = self.slots.acquire(cancel=cancel, kind=kind, label=source, on_wait=note_wait)
            slot.__enter__()
        except SlotsBusy:
            if cancel and cancel():
                raise Cancelled() from None          # stopped while waiting for a slot (QA B-6)
            raise
        if waited and on_wait:
            _safe(on_wait, 0)                        # 0 = got a slot; the model starts now (QA B-1)
        from .qwen_log import WireResponse
        wire = None
        try:
            if cancel and cancel():
                raise Cancelled()
            t0 = time.perf_counter()
            try:
                resp = open_request(req, timeout=self.timeout)
                wire = WireResponse(resp)
            except urllib.error.HTTPError as e:
                try:
                    text = e.read().decode("utf-8", "replace")
                except Exception:  # noqa: BLE001
                    text = ""
                text = self._scrub(text, key)[:800]
                self._record(None, False, source)
                raise ApiError(e.code, explain(e.code, text), error_code(ApiError(e.code, text))) from None
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                msg = self._scrub(str(getattr(e, "reason", e)), key)
                self._record(None, False, source)
                raise ApiError(None, msg, explain(None, msg)) from None
            # Stop works at once: a watcher closes the connection while we wait for the first token (QA B-6)
            done = threading.Event()

            def watch():
                while not done.wait(0.2):
                    if cancel and cancel():
                        try:
                            resp.close()
                        except Exception:  # noqa: BLE001
                            pass
                        return
            if cancel:
                threading.Thread(target=watch, daemon=True).start()
            r = Reply(finished=False)
            try:
                with resp:
                    r = read_stream(wire, on_reasoning=on_reasoning, on_content=on_content, cancel=cancel, reply=r)
            except Cancelled:
                self._record(None, False, source)
                raise
            except ApiError:
                self._record(None, False, source)
                raise
            except (urllib.error.URLError, TimeoutError, OSError, ValueError, AttributeError) as e:
                if cancel and cancel():
                    self._record(None, False, source)
                    raise Cancelled() from None
                msg = self._scrub(str(getattr(e, "reason", e)), key)
                self._record(None, False, source)
                raise ApiError(None, msg, explain(None, msg)) from None
            finally:
                done.set()
                if getattr(self, "qwen_log", None):
                    from .qwen_log import caller_id
                    self.qwen_log.write("Bot", caller_id.get(), self.model, r)
            r.elapsed_s = time.perf_counter() - t0
        finally:
            slot.__exit__(None, None, None)
        if cancel and cancel():
            self._record(r, False, source)
            raise Cancelled()
        if not r.finished:
            self._record(r, False, source)
            raise StreamCut(r)
        self._record(r, True, source)
        if self.redactor:
            for c in r.tool_calls:
                c["arguments"] = self.redactor.restore_paths(c["arguments"])
        return r


def read_stream(resp, on_reasoning=None, on_content=None, cancel=None, reply=None) -> Reply:
    """Read an SSE stream of chat.completion.chunk objects into a Reply, calling the callbacks per delta."""
    r = reply if reply is not None else Reply(finished=False)
    calls: dict[int, dict] = {}
    for raw in resp:
        if cancel and cancel():
            raise Cancelled()
        line = raw.decode("utf-8", "replace").strip() if isinstance(raw, bytes) else str(raw).strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            r.finished = bool(r.finish_reason)
            break
        try:
            ev = json.loads(data)
        except json.JSONDecodeError:
            continue
        if ev.get("error"):
            raise stream_error(ev["error"])
        u = ev.get("usage")
        if u:
            r.input_tokens = u.get("prompt_tokens") or r.input_tokens
            r.output_tokens = u.get("completion_tokens") or r.output_tokens
            r.reasoning_tokens = (u.get("completion_tokens_details") or {}).get("reasoning_tokens", r.reasoning_tokens)
        for ch in ev.get("choices") or []:
            d = ch.get("delta") or {}
            think = d.get("reasoning_content") or d.get("reasoning") or ""
            if think:
                r.reasoning += think
                if on_reasoning:
                    _safe(on_reasoning, think)
            piece = d.get("content") or ""
            if piece:
                r.content += piece
                if on_content:
                    _safe(on_content, piece)
            for tc in tool_fragments(d):
                c = calls.setdefault(int(tc.get("index", 0) or 0), {"id": None, "name": "", "arguments": ""})
                c["id"] = tc.get("id") or c["id"]
                fn = tc.get("function") or {}
                c["name"] += fn.get("name") or ""
                c["arguments"] += fn.get("arguments") or ""
            r.tool_calls = list(calls.values())
            if ch.get("finish_reason"):
                r.finish_reason = ch["finish_reason"]
                r.finished = True
    if not r.reasoning and "<think>" in r.content:
        r.reasoning = "\n\n".join(m.strip() for m in THINK_RE.findall(r.content))
        r.content = THINK_RE.sub("", r.content)
    r.content = r.content.strip()
    if r.finish_reason not in ("stop", "tool_calls", "function_call"):
        r.finished = False
    r.tool_calls = [{"id": c["id"] or f"call_{i}", "name": c["name"], "arguments": c["arguments"] or "{}"}
                    for i, c in sorted(calls.items()) if c["name"]]
    return r


def _safe(fn, *a):
    try:
        fn(*a)
    except Exception:  # noqa: BLE001 - UI callbacks must not break the stream
        pass


class FakeLLM:
    """Scripted model for tests. responder(messages, tools, effort) -> dict(content, reasoning, tool_calls)."""

    is_real = False

    def __init__(self, responder):
        self.responder = responder
        self.calls: list[dict] = []
        self._lock = threading.Lock()

    def has_key(self) -> bool:
        return True

    def stream_chat(self, messages, *, tools=None, effort="low", max_tokens=4096, on_reasoning=None,
                    on_content=None, cancel=None, source="chat", kind="chat", on_wait=None, tool_choice="auto") -> Reply:
        snap = json.loads(json.dumps(messages, ensure_ascii=False))
        with self._lock:
            self.calls.append({"messages": snap, "tools": [t["function"]["name"] for t in tools or []],
                               "effort": effort, "source": source, "kind": kind, "tool_choice": tool_choice if tools else None})
        out = self.responder(snap, tools, effort) or {}
        think = out.get("reasoning", "")
        if think and on_reasoning:
            for i in range(0, len(think), 8):
                on_reasoning(think[i:i + 8])
        text = out.get("content", "")
        if text and on_content:
            for i in range(0, len(text), 6):
                on_content(text[i:i + 6])
        calls = [{"id": f"call_{i}", "name": c["name"],
                  "arguments": c["arguments"] if isinstance(c["arguments"], str) else json.dumps(c["arguments"], ensure_ascii=False)}
                 for i, c in enumerate(out.get("tool_calls") or [])]
        if out.get("cut"):
            raise StreamCut(Reply(content=text, reasoning=think, finished=False))
        if out.get("error_status") is not None:
            raise ApiError(out["error_status"] or None, out.get("error_text", "error"), explain(out["error_status"] or None, out.get("error_text", "")))
        return Reply(content=text, reasoning=think, tool_calls=calls, finish_reason="tool_calls" if calls else "stop",
                     input_tokens=out.get("input_tokens", 10), output_tokens=out.get("output_tokens", 5))
