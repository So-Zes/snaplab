"""Translator for the Codex harness: Codex speaks the Responses API, the Snapscribe server only speaks
Chat Completions. This converts requests and streams the answers back as Responses events.
The conversion functions below come from an earlier research translator.

    Codex --Responses--> 127.0.0.1:<proxy.port> (this, inside the SnapLab process) --Chat Completions--> Snapscribe

- Thinking depth: Responses reasoning.effort becomes chat_template_kwargs.reasoning_effort (low / medium / xhigh;
  minimal->low, high->xhigh, none->enable_thinking false). The default is medium.
- Earlier thinking that Codex sends back is attached to the next assistant message as reasoning_content.
- Images are passed as image_url parts.
- Codex authenticates with a random per-process token (SNAPLAB_PROXY_TOKEN); the real API key never leaves SnapLab.
- Every upstream request holds one shared API slot. Nothing is retried here.
- Privacy filter: private values (home folder, user name, machine and tailnet names) are replaced before the request
  leaves the PC; the home-folder placeholder in tool-call arguments is mapped back to the real path.
- The log (<data>/logs/proxy.log) has one line per request with sizes and timings, never content or keys.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
import time
import urllib.error
import urllib.request
from .provider_protocol import open_request, prepare, tool_fragments
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULTS = {"default_effort": "medium", "keep_reasoning": True, "pass_images": True}
EFFORT_MAP = {"minimal": "low", "low": "low", "medium": "medium", "high": "xhigh", "xhigh": "xhigh"}
EFFORT_CHOICES = (*EFFORT_MAP, "none", "server")
SETTINGS = dict(DEFAULTS)


def iter_sse(resp, *, require_done=False):
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            return
        try:
            yield json.loads(data)
        except json.JSONDecodeError:
            continue
    if require_done:
        raise OSError("upstream stream ended before completion")


def upstream_events(resp):
    """Distinguish an upstream reset from a downstream client disconnect."""
    try:
        yield from iter_sse(resp, require_done=True)
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as exc:
        raise OSError("upstream connection closed before completion") from exc


# ---------- Responses -> Chat Completions (copied from the research translator) ----------

IMAGE_TYPES = ("input_image", "image_url")


def text_of(content) -> str:
    """Content as text only (for system, assistant and the text part of tool output). An image becomes a note that it was left out."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for c in content:
        if isinstance(c, str):
            parts.append(c)
            continue
        t = c.get("type")
        if t in ("input_text", "output_text", "text", "summary_text", "reasoning_text"):
            parts.append(c.get("text", ""))
        elif t in IMAGE_TYPES:
            parts.append("[image omitted]")
        elif t == "input_file":
            parts.append("[file omitted]")
    return "\n\n".join(p for p in parts if p)


def image_part(c: dict) -> dict | None:
    """A Responses input_image as a Chat Completions image_url part. None when there is no URL (only a file_id)."""
    url = c.get("image_url")
    if isinstance(url, dict):
        url = url.get("url")
    if not isinstance(url, str) or not url:
        return None
    return {"type": "image_url", "image_url": {"url": url}}


def _parts(content) -> list[dict]:
    """Content as a list of text and image parts, in order."""
    parts = []
    for c in content:
        if isinstance(c, dict) and c.get("type") in IMAGE_TYPES:
            parts.append(image_part(c) or {"type": "text", "text": "[image omitted: no URL]"})
        else:
            t = text_of([c])
            if t:
                parts.append({"type": "text", "text": t})
    return parts


def has_image(content) -> bool:
    return isinstance(content, list) and any(isinstance(c, dict) and c.get("type") in IMAGE_TYPES for c in content)


def reasoning_text(it: dict) -> str:
    """The thinking text of a Responses reasoning item (this translator returns the full text in summary)."""
    summary, content = text_of(it.get("summary")), text_of(it.get("content"))
    if summary and content and summary != content:
        return f"{summary}\n\n{content}"
    return summary or content


def effort_kwargs(requested: str | None, default: str) -> tuple[dict | None, str]:
    """Effort -> (chat_template_kwargs, name for the log). A missing or unknown value uses the default."""
    eff = (requested or "").lower()
    if eff not in EFFORT_CHOICES:
        eff = default
    if eff == "none":
        return {"enable_thinking": False}, "thinking_off"
    if eff in EFFORT_MAP:
        return {"reasoning_effort": EFFORT_MAP[eff]}, EFFORT_MAP[eff]
    return None, "server_default"  # server: send nothing (the server default applies)



def _short_error(text: str) -> str:
    """The provider's own message from a JSON error body (HTML error pages give nothing)."""
    try:
        data = json.loads(text)
        msg = (data.get("error") or {}).get("message") if isinstance(data.get("error"), dict) else data.get("error")
        return " ".join(str(msg or "").split())[:160]
    except (ValueError, AttributeError, TypeError):
        return ""

class ToolMap:
    """Keep Responses identities separate from the flat Chat tool names for one request."""

    def __init__(self, body: dict):
        self.definitions = []
        for tool in body.get("tools") or []:
            if tool.get("type") == "namespace":
                for child in tool.get("tools") or []:
                    if child.get("type") in ("function", "custom"):
                        self.definitions.append((tool["name"], child, tool.get("description", "")))
            elif tool.get("type") in ("function", "custom"):
                self.definitions.append((None, tool, ""))

        identities = {self.identity(namespace, tool["name"]) for namespace, tool, _ in self.definitions}
        # Calls in history still need the same flat identity, even if a tool was removed.
        identities.update(self.identity(item.get("namespace"), item["name"])
                          for item in body.get("input") or []
                          if item.get("type") in ("function_call", "custom_tool_call"))
        self.aliases = {(ns, name): name for ns, name in identities if ns is None}
        used = set(self.aliases.values())
        for ns, name in sorted((ns, name) for ns, name in identities if ns is not None):
            stem = re.sub(r"[^A-Za-z0-9_-]", "_", f"{ns}__{name}")
            alias = stem
            if len(alias) > 64 or alias in used:
                digest = hashlib.sha256(json.dumps([ns, name]).encode()).hexdigest()[:12]
                alias = f"{stem[:50]}__{digest}"
                suffix = 0
                while alias in used:
                    suffix += 1
                    tail = f"__{digest}_{suffix}"
                    alias = stem[:64 - len(tail)] + tail
            self.aliases[ns, name] = alias
            used.add(alias)
        self.originals = {alias: identity for identity, alias in self.aliases.items()}
        self.custom = {self.chat_name(namespace, tool["name"]) for namespace, tool, _ in self.definitions
                       if tool["type"] == "custom"}

    @staticmethod
    def identity(namespace, name):
        # Codex treats an absent, empty, or "functions" namespace as the default.
        return (None if namespace in (None, "", "functions") else namespace, name)

    def chat_name(self, namespace, name):
        return self.aliases.get(self.identity(namespace, name), name)

    def restore_call(self, item: dict, alias: str) -> None:
        namespace, name = self.originals.get(alias, (None, alias))
        item["name"] = name
        if namespace is not None:
            item["namespace"] = namespace


def to_chat(body: dict, settings: dict | None = None) -> tuple[dict, ToolMap, dict]:
    """Turn a Responses request into a Chat Completions request.

    Returns the body, the map for restoring tool names, and counts for the log. Without settings, SETTINGS is used.
    """
    cfg = {**DEFAULTS, **(SETTINGS if settings is None else settings)}
    keep, pass_images = cfg["keep_reasoning"], cfg["pass_images"]
    tool_map = ToolMap(body)
    system_parts = [body["instructions"]] if body.get("instructions") else []
    msgs: list[dict] = []
    started = False      # whether the first user / assistant message has appeared (system messages are merged at the top)
    declared = set()     # call_ids whose calls were declared
    pending = ""         # thinking not yet attached to an assistant message
    tool_images: list[dict] = []  # images from tool output (collected while tool results follow each other, then sent as user)
    st = {"reasoning_items": 0, "reasoning_attached": 0, "reasoning_chars_sent": 0, "images": 0}

    def attach(m: dict) -> None:
        nonlocal pending
        if keep and pending:
            m["reasoning_content"] = (m.get("reasoning_content", "") + "\n\n" + pending).strip()
            st["reasoning_attached"] += 1
            st["reasoning_chars_sent"] += len(pending)
        pending = ""

    def flush_tool_images() -> None:
        # Chat Completions tool messages cannot carry images, so they follow the run of tool results as a user message
        if tool_images:
            msgs.append({"role": "user", "content": [{"type": "text", "text": "[images from the tool output above]"},
                                                     *tool_images]})
            tool_images.clear()

    for it in body.get("input") or []:
        t = it.get("type") or ("message" if "role" in it else None)
        if t not in ("function_call_output", "custom_tool_call_output"):
            flush_tool_images()
        if t == "reasoning":
            st["reasoning_items"] += 1
            r = reasoning_text(it)
            if r:
                pending = (pending + "\n\n" + r).strip()
            continue
        if t == "message":
            role = it.get("role")
            if role in ("system", "developer"):
                text = text_of(it.get("content"))
                if not started:
                    system_parts.append(text)
                else:
                    # instructions in the middle go as user messages, which every chat template accepts
                    msgs.append({"role": "user", "content": f"<{role}_message>\n{text}\n</{role}_message>"})
            elif role == "assistant":
                started = True
                text = text_of(it.get("content"))
                if text:
                    m = {"role": "assistant", "content": text}
                    attach(m)
                    msgs.append(m)
            else:
                started = True
                pending = ""  # thinking followed by a user message without an answer has nothing to attach to: drop it
                content = it.get("content")
                if pass_images and has_image(content):
                    content = _parts(content)
                    st["images"] += sum(1 for c in content if c.get("type") == "image_url")
                else:
                    content = text_of(content)
                msgs.append({"role": "user", "content": content})
        elif t in ("function_call", "custom_tool_call"):
            started = True
            call_id = it.get("call_id") or it.get("id") or f"call_{uuid.uuid4().hex[:12]}"
            if t == "function_call":
                args = it.get("arguments") or "{}"
            else:
                args = json.dumps({"input": it.get("input", "")}, ensure_ascii=False)
            call = {"id": call_id, "type": "function", "function": {
                "name": tool_map.chat_name(it.get("namespace"), it.get("name")), "arguments": args}}
            declared.add(call_id)
            last = msgs[-1] if msgs else None
            if last and last["role"] == "assistant":
                last.setdefault("tool_calls", []).append(call)
                attach(last)
            else:
                m = {"role": "assistant", "content": None, "tool_calls": [call]}
                attach(m)
                msgs.append(m)
        elif t in ("function_call_output", "custom_tool_call_output"):
            out = it.get("output")
            if isinstance(out, dict):
                out = out.get("content") if isinstance(out.get("content"), (str, list)) else json.dumps(out, ensure_ascii=False)
            if pass_images and has_image(out):
                parts = _parts(out)
                images = [p for p in parts if p["type"] == "image_url"]
                out = "\n\n".join(p["text"] for p in parts if p["type"] == "text") or "[image]"
                st["images"] += len(images)
                tool_images.extend(images)
            out = out if isinstance(out, str) else text_of(out)
            cid = it.get("call_id")
            if cid in declared:
                msgs.append({"role": "tool", "tool_call_id": cid, "content": out})
            else:
                msgs.append({"role": "user", "content": f"[tool output for {cid}]\n{out}"})
        # kinds this server cannot use (web search results and the like) are not sent
    flush_tool_images()

    limit = max(0, int(cfg.get('image_history_limit', 0)))
    if limit:
        remaining = limit
        dropped = 0
        for message in reversed(msgs):
            if isinstance(message.get('content'), list):
                for index in range(len(message['content']) - 1, -1, -1):
                    part = message['content'][index]
                    if part.get('type') == 'image_url':
                        if remaining:
                            remaining -= 1
                        else:
                            message['content'][index] = {'type':'text','text':'[Earlier screenshot omitted; use the latest screenshot.]'}
                            dropped += 1
        st['images'] -= dropped
        st['images_omitted'] = dropped

    if system_parts:
        msgs.insert(0, {"role": "system", "content": "\n\n".join(system_parts)})

    tools = []
    for namespace, tool, namespace_description in tool_map.definitions:
        name = tool_map.chat_name(namespace, tool["name"])
        description = tool.get("description", "")
        if namespace_description:
            description = f"{namespace_description}\n\n{description}"
        if tool.get("type") == "function":
            tools.append({"type": "function", "function": {
                "name": name, "description": description,
                "parameters": tool.get("parameters") or {"type": "object", "properties": {}}}})
        elif tool.get("type") == "custom":
            tools.append({"type": "function", "function": {
                "name": name,
                "description": description + "\n\nPut the whole raw input in the `input` string.",
                "parameters": {"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]}}})
        if "strict" in tool:
            tools[-1]["function"]["strict"] = tool["strict"]

    chat: dict = {"messages": msgs}
    if tools:
        chat["tools"] = tools
        tc = body.get("tool_choice", "auto")
        if isinstance(tc, dict) and tc.get("name"):
            tc = {"type": "function", "function": {"name": tool_map.chat_name(tc.get("namespace"), tc["name"])}}
        chat["tool_choice"] = tc if tc in ("auto", "none", "required") or isinstance(tc, dict) else "auto"
        if "parallel_tool_calls" in body:
            chat["parallel_tool_calls"] = bool(body["parallel_tool_calls"])
    for src, dst in (("max_output_tokens", "max_tokens"), ("temperature", "temperature"), ("top_p", "top_p")):
        if body.get(src) is not None:
            chat[dst] = body[src]
    fmt = (body.get("text") or {}).get("format") or {}
    if fmt.get("type") == "json_schema":
        chat["response_format"] = {"type": "json_schema", "json_schema": {
            "name": fmt.get("name", "output"), "schema": fmt.get("schema", {}), "strict": bool(fmt.get("strict"))}}
    # thinking depth: a top-level reasoning_effort is dropped by the server, so it goes in as a template argument
    requested = (body.get("reasoning") or {}).get("effort")
    kwargs, sent = effort_kwargs(requested, cfg["default_effort"])
    if kwargs:
        chat["chat_template_kwargs"] = kwargs
    st.update(effort_requested=requested or None, effort_sent=sent)
    return chat, tool_map, st


# ---------- Chat Completions stream -> Responses events ----------

class Emitter:
    def __init__(self, wfile):
        self.w, self.seq = wfile, 0
        self.restore = None

    def send(self, type_: str, **data) -> None:
        data = {"type": type_, "sequence_number": self.seq, **data}
        self.seq += 1
        self.w.write(f"event: {type_}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8"))
        self.w.flush()


class _ResponseCollector(Emitter):
    """Reuse the stream conversion when the caller requests a single JSON response."""

    def __init__(self):
        super().__init__(None)
        self.response = None

    def send(self, type_: str, **data) -> None:
        if type_ in ("response.completed", "response.incomplete"):
            self.response = data["response"]


def stream_back(em: Emitter, upstream, model: str, tool_map: ToolMap, t0: float, capture=None) -> dict:
    """Read the server's answer and send events to Codex as it arrives. Returns counts for the log; t0 is the time just before sending."""
    rid = f"resp_{uuid.uuid4().hex}"
    em.send("response.created", response={"id": rid, "object": "response", "status": "in_progress", "model": model, "output": []})
    em.send("response.in_progress", response={"id": rid, "object": "response", "status": "in_progress", "model": model, "output": []})

    output: list[dict] = []
    reasoning = {"id": f"rs_{uuid.uuid4().hex[:16]}", "text": "", "open": False, "idx": None}
    message = {"id": f"msg_{uuid.uuid4().hex[:16]}", "text": "", "open": False, "idx": None}
    calls: dict[int, dict] = {}
    usage, finish, t_first = {}, None, None
    pending_ws = ""  # whitespace before the text starts; a reply of only whitespace is not passed to Codex

    def close_reasoning():
        if reasoning["open"]:
            item = {"type": "reasoning", "id": reasoning["id"],
                    "summary": [{"type": "summary_text", "text": reasoning["text"]}], "encrypted_content": None}
            em.send("response.reasoning_summary_text.done", item_id=reasoning["id"], output_index=reasoning["idx"],
                    summary_index=0, text=reasoning["text"])
            em.send("response.output_item.done", output_index=reasoning["idx"], item=item)
            output.append(item)
            reasoning["open"] = False

    for ev in upstream_events(upstream):
        if capture is not None:
            capture.observe(ev)
        if ev.get("error"):
            from .llm import stream_error
            raise stream_error(ev["error"])
        if ev.get("usage"):
            usage = ev["usage"]
        for ch in ev.get("choices") or []:
            d = ch.get("delta") or {}
            finish = ch.get("finish_reason") or finish
            think = d.get("reasoning_content") or d.get("reasoning") or ""
            piece = d.get("content") or ""
            if (think or piece or d.get("tool_calls")) and t_first is None:
                t_first = time.perf_counter()
            if think:
                if not reasoning["open"] and not reasoning["text"]:
                    reasoning.update(open=True, idx=len(output))
                    em.send("response.output_item.added", output_index=reasoning["idx"],
                            item={"type": "reasoning", "id": reasoning["id"], "summary": []})
                    em.send("response.reasoning_summary_part.added", item_id=reasoning["id"],
                            output_index=reasoning["idx"], summary_index=0, part={"type": "summary_text", "text": ""})
                if reasoning["open"]:
                    reasoning["text"] += think
                    em.send("response.reasoning_summary_text.delta", item_id=reasoning["id"],
                            output_index=reasoning["idx"], summary_index=0, delta=think)
            if piece and not message["open"]:
                if not (pending_ws + piece).strip():
                    pending_ws += piece
                    piece = ""
                else:
                    piece = (pending_ws + piece).lstrip()
                    pending_ws = ""
            if piece:
                close_reasoning()
                if not message["open"]:
                    message.update(open=True, idx=len(output))
                    em.send("response.output_item.added", output_index=message["idx"],
                            item={"type": "message", "id": message["id"], "role": "assistant", "status": "in_progress", "content": []})
                    em.send("response.content_part.added", item_id=message["id"], output_index=message["idx"],
                            content_index=0, part={"type": "output_text", "text": "", "annotations": []})
                message["text"] += piece
                em.send("response.output_text.delta", item_id=message["id"], output_index=message["idx"],
                        content_index=0, delta=piece)
            for tc in tool_fragments(d):
                close_reasoning()
                c = calls.setdefault(tc.get("index", 0), {"id": None, "name": "", "args": ""})
                c["id"] = tc.get("id") or c["id"]
                fn = tc.get("function") or {}
                c["name"] += fn.get("name") or ""
                c["args"] += fn.get("arguments") or ""

    # [DONE] only proves that transport ended cleanly, not that generation succeeded.
    incomplete_reason = None
    if finish == "length":
        incomplete_reason = "max_output_tokens"
    elif finish == "content_filter":
        incomplete_reason = "content_filter"
    elif finish not in ("stop", "tool_calls", "function_call"):
        raise ValueError("upstream did not report a successful generation finish")
    close_reasoning()
    if message["open"]:
        content = [{"type": "output_text", "text": message["text"], "annotations": []}]
        em.send("response.output_text.done", item_id=message["id"], output_index=message["idx"], content_index=0, text=message["text"])
        em.send("response.content_part.done", item_id=message["id"], output_index=message["idx"], content_index=0, part=content[0])
        item = {"type": "message", "id": message["id"], "role": "assistant",
                "status": "incomplete" if incomplete_reason else "completed", "content": content}
        em.send("response.output_item.done", output_index=message["idx"], item=item)
        output.append(item)
    # Calls are buffered until generation succeeds. Even syntactically valid arguments
    # from an incomplete generation must not become executable output items.
    for _, c in ([] if incomplete_reason else sorted(calls.items())):
        call_id = c["id"] or f"call_{uuid.uuid4().hex[:12]}"
        args = c["args"].strip() or "{}"
        if getattr(em, "restore", None):
            args = em.restore(args)   # SnapLab: map the home-folder placeholder back to the real path
        if c["name"] in tool_map.custom:
            try:
                raw = json.loads(args).get("input", "")
            except (json.JSONDecodeError, AttributeError):
                raw = args
            item = {"type": "custom_tool_call", "id": f"ctc_{uuid.uuid4().hex[:16]}", "call_id": call_id,
                    "name": c["name"], "input": raw, "status": "completed"}
        else:
            item = {"type": "function_call", "id": f"fc_{uuid.uuid4().hex[:16]}", "call_id": call_id,
                    "name": c["name"], "arguments": args, "status": "completed"}
        tool_map.restore_call(item, c["name"])
        idx = len(output)
        em.send("response.output_item.added", output_index=idx, item={**item, "status": "in_progress"})
        em.send("response.output_item.done", output_index=idx, item=item)
        output.append(item)

    in_tok, out_tok = usage.get("prompt_tokens") or 0, usage.get("completion_tokens") or 0
    cached = ((usage.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0
    think_tok = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")  # may be missing in a stream
    status = "incomplete" if incomplete_reason else "completed"
    response = {
        "id": rid, "object": "response", "status": status, "model": model, "output": output,
        "usage": {"input_tokens": in_tok, "input_tokens_details": {"cached_tokens": cached},
                  "output_tokens": out_tok, "output_tokens_details": {"reasoning_tokens": think_tok or 0},
                  "total_tokens": in_tok + out_tok}}
    if incomplete_reason:
        response["incomplete_details"] = {"reason": incomplete_reason}
    em.send("response." + status, response=response)
    t_end = time.perf_counter()
    return {"input_tokens": in_tok, "output_tokens": out_tok, "reasoning_tokens": think_tok, "finish_reason": finish,
            "ttft_s": round(t_first - t0, 2) if t_first else None, "total_s": round(t_end - t0, 2),
            "status": status, "tool_calls": 0 if incomplete_reason else len(calls),
            "text_chars": len(message["text"]), "reasoning_chars": len(reasoning["text"])}



# ---------- HTTP (SnapLab version) ----------

class _Handler(BaseHTTPRequestHandler):
    def do_CONNECT(self):
        """Optional per-job WSL Internet tunnel; never enabled on other translators."""
        from urllib.parse import urlsplit
        from .harnesses.wsl_network import pump
        t = self.owner
        got = self.headers.get('Proxy-Authorization', '').removeprefix('Bearer ')
        if t.egress is None or not hmac.compare_digest(got.encode(), t.token.encode()):
            return self._json(403, {'error': 'forbidden'})
        try:
            target = urlsplit('//' + self.path)
            if target.username or target.password or target.path or target.query or target.fragment:
                raise ValueError('invalid destination')
            with t.egress(target.hostname, target.port or 443) as upstream:
                self.send_response(200, 'Connection Established')
                self.end_headers()
                self.wfile.flush()
                pump(self.connection, upstream)
        except (OSError, ValueError, TypeError, RuntimeError):
            self.close_connection = True
            try:
                self._json(403, {'error': 'forbidden'})
            except OSError:
                pass

    protocol_version = "HTTP/1.0"
    server_version = "snaplab-translator"
    owner = None

    def setup(self):
        # No read deadline (2026-11-16): the only callers are this PC's own workers through a loopback port with a
        # random token, and their requests carry screenshots over links that can be slow; a fixed limit cut real work.
        self.request.settimeout(None)
        super().setup()

    def finish(self):
        super().finish()

    def log_message(self, *a):
        pass

    def _json(self, status: int, obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.owner.web_mcp and self.owner.web_mcp.handle_http(self):
            return
        path = self.path.split("?")[0].rstrip("/")
        if path == "/health":
            return self._json(200, {"ok": True, "app": "snaplab-translator"})
        if path == "/v1/models":
            return self._json(200, {"object": "list", "data": [{"id": self.owner.model, "object": "model"}]})
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        t = self.owner
        if t.web_mcp and t.web_mcp.handle_http(self):
            return
        path = self.path.split("?")[0].rstrip("/")
        if path != "/v1/responses":
            return self._json(404, {"error": {"message": "not found"}})
        got = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(got.encode(), t.token.encode()):
            t.log({"status": 401})
            return self._json(401, {"error": {"message": "invalid token", "type": "invalid_request_error"}})
        if t.cancelled.is_set():
            return self._json(503, {"error": {"message": "translator stopped"}})
        lengths = self.headers.get_all("Content-Length") if hasattr(self.headers, "get_all") else [self.headers.get("Content-Length")]
        try:
            if self.headers.get("Transfer-Encoding") or len(lengths or []) != 1:
                raise ValueError("unsupported framing")
            n = int(lengths[0])
            if n <= 0:
                raise ValueError("invalid length")
        except (ValueError, TypeError):
            return self._json(400, {"error": {"message": "invalid request framing"}})
        try:
            raw = self.rfile.read(n)
        except (OSError, TimeoutError):
            return self._json(408, {"error": {"message": "request read timed out"}})
        if len(raw) != n:
            return self._json(400, {"error": {"message": "incomplete request"}})
        try:
            body_peek = json.loads(raw)
        except ValueError:
            body_peek = {}
        try:
            tg = t.target(body_peek.get("model"))
        except ValueError:
            return self._json(400, {"error": {"message": "The selected model is no longer configured."}})
        key = tg["key"]
        from .guard import Guard
        guard = t.redactor.guard if t.redactor else Guard()
        env = tg.get("api_key_env")
        if env and env not in guard.extra_env:
            guard.extra_env.append(env)
        guard.add(key)
        if not key:
            return self._json(500, {"error": {"message": "API key not set on the SnapLab PC", "type": "server_error"}})
        rec: dict = {"model": tg["name"]}
        try:
            body = json.loads(raw)
            chat, tool_map, st = to_chat(body, {**SETTINGS, 'image_history_limit': getattr(t, 'image_history_limit', 0)})
        except Exception as e:  # noqa: BLE001
            message = guard.clean(str(e))
            t.log({"status": 400, "error": f"conversion failed: {message}"[:200]})
            return self._json(400, {"error": {"message": f"translator could not convert request: {message}",
                                              "type": "invalid_request_error"}})
        if getattr(t, "output_limit", 0):
            chat['max_tokens'] = min(max(1, int(chat.get('max_tokens') or t.output_limit)), t.output_limit)
        if getattr(t, "effort", None):
            chat['chat_template_kwargs'], st['effort_sent'] = effort_kwargs(t.effort, 'medium')
        if t.redactor:
            t.redactor.add_secret(key)
            chat = t.redactor.clean_obj(chat)
        rec.update(messages=len(chat["messages"]), images=st.get('images', 0), tools=len(chat.get("tools", [])), effort=st.get("effort_sent"), tool_choice=chat.get("tool_choice"),
                   tool_names=[(t.get("function") or {}).get("name") for t in chat.get("tools", [])][:20],
                   # tool kinds Codex sent, so a kind the conversion drops (MCP tools, for example) shows up in the log
                   sent_tool_kinds=sorted({str(x.get("type")) for x in body.get("tools") or []}))
        chat = {**chat, "model": tg["model"], "stream": True, "stream_options": {"include_usage": True}}
        for k_, v_ in (tg.get("extra") or {}).items():
            chat.setdefault(k_, v_)
        ctk = chat.pop("chat_template_kwargs", None)
        if ctk and tg.get("effort_style", "chat_template_kwargs") == "chat_template_kwargs":
            chat["chat_template_kwargs"] = ctk
        elif ctk and tg.get("effort_style") == "reasoning_effort" and ctk.get("reasoning_effort"):
            chat["reasoning_effort"] = {"xhigh": "high"}.get(ctk["reasoning_effort"], ctk["reasoning_effort"])
        if tg.get("effort_style", "none") != "chat_template_kwargs":
            chat["messages"] = [{k: v for k, v in m.items() if k != "reasoning_content"} for m in chat["messages"]]
        chat = guard.clean_obj(chat)
        try:
            chat = prepare(chat, tg)
        except Exception as exc:
            from .llm import error_code
            from .i18n import text as translated
            return self._json(400, {'error': {'message': translated(tg.get('error_language', 'en'), 'err.code.' + error_code(exc))}})
        req = urllib.request.Request(f"{tg['base_url']}/chat/completions", data=json.dumps(chat).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": tg["user_agent"], "Accept": "text/event-stream"})
        req.add_unredirected_header("Authorization", f"Bearer {key}")
        if getattr(t, 'ready', None) is not None and not t.ready.wait(10):
            return self._json(503, {'error': {'message': 'Worker safety initialization was not confirmed.'}})
        t0 = time.perf_counter()
        wire = None
        from .slots import SlotsBusy
        try:
            slot = tg["slots"].acquire(kind="session", label="codex", cancel=t.cancelled.is_set)
            slot.__enter__()
        except SlotsBusy:
            return self._json(503, {"error": {"message": "translator stopped"}})
        try:
            if t.cancelled.is_set():
                return self._json(503, {"error": {"message": "translator stopped"}})
            rec["slot_wait_s"] = round(time.perf_counter() - t0, 2)
            if getattr(t, "max_calls", 0):
                with t._request_lock:
                    fingerprint = hashlib.sha256(req.data).hexdigest()
                    if t.max_calls and (t.calls >= t.max_calls or fingerprint in t._requests or t.uncertain):
                        t.call_limit_reached = t.calls >= t.max_calls
                        t.log({'kind': 'request.refused', 'session_id': t.session_id, 'reason': 'call_limit' if t.call_limit_reached else 'duplicate_or_uncertain', 'calls': t.calls})
                        return self._json(409, {'error': {'message': 'Job request limit, duplicate request, or uncertain previous request.'}})
                    t._requests.add(fingerprint)
                    t.calls += 1
            t_send = time.perf_counter()
            try:
                upstream = open_request(req, timeout=float(tg.get("timeout_s") or t.timeout))
                with t._active_lock:
                    if t.cancelled.is_set():
                        upstream.close()
                        return self._json(503, {"error": {"message": "translator stopped"}})
                    t._active.add(upstream)
            except urllib.error.HTTPError as e:
                if e.code >= 500:
                    t.uncertain = True
                try:
                    text = guard.clean(e.read().decode("utf-8", "replace"))
                except Exception:  # noqa: BLE001
                    text = ""
                text = text.replace(key, "***")
                from .llm import ApiError, error_code
                from .i18n import text as translated
                err = {"message": translated(tg.get("error_language", "en"), "err.code." + error_code(ApiError(e.code, text))), "type": "invalid_request_error" if e.code < 500 else "server_error"}
                if e.code == 400 and "context" in text.lower():
                    err["code"] = "context_length_exceeded"
                t.last_error = f"model server answered {e.code}" + (": " + _short_error(text) if _short_error(text) else "")
                t.log({**rec, "status": e.code, "error_body": text, "session_id": getattr(t, 'session_id', '')})
                t.usage(0, 0, False, tg['name'])
                return self._json(e.code, {"error": err})
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                t.uncertain = True
                t.last_error = "model server unreachable: " + guard.clean(str(getattr(e, "reason", e)))[:120]
                t.log({**rec, "status": 502, "error": guard.clean(str(getattr(e, "reason", e)))[:200]})
                t.usage(0, 0, False, tg['name'])
                return self._json(502, {"error": {"message": "upstream unreachable", "type": "server_error"}})
            streaming = body.get("stream", True)
            if streaming:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
            em = Emitter(self.wfile) if streaming else _ResponseCollector()
            em.restore = t.redactor.restore_paths if t.redactor and getattr(t, "restore_paths", True) else None
            from .qwen_log import Capture
            capture = Capture()
            from .qwen_log import WireResponse
            wire = WireResponse(upstream)
            try:
                with upstream:
                    stats = stream_back(em, wire, body.get("model") or tg["model"], tool_map, t_send, capture)
                t.incomplete = getattr(t, 'incomplete', False) or stats['status'] != 'completed'
                if stats.get('finish_reason') == 'length':
                    t.length_count = getattr(t, 'length_count', 0) + 1
                if not streaming:
                    self._json(200, em.response)
                t.log({**rec, "status": 200, **{k: stats.get(k) for k in ("input_tokens", "output_tokens", "ttft_s",
                                                                           "total_s", "tool_calls", "finish_reason")},
                       "response_status": stats["status"]})
                t.usage(stats.get("input_tokens") or 0, stats.get("output_tokens") or 0,
                        stats["status"] == "completed", tg['name'])
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                t.uncertain = True
                t.log({**rec, "status": 499, "error": "client closed the connection"})
                t.usage(0, 0, False, tg['name'])
            except Exception as e:  # noqa: BLE001
                t.uncertain = True
                msg = guard.clean(str(e))[:200]
                t.log({**rec, "status": 502, "error": msg})
                t.usage(0, 0, False, tg['name'])
                try:
                    error = {"code": "server_error", "message": msg}
                    if streaming:
                        em.send("response.failed", response={"id": f"resp_{uuid.uuid4().hex}", "status": "failed",
                                                             "error": error})
                    else:
                        self._json(502, {"error": error})
                except OSError:
                    pass
            finally:
                if getattr(t, "qwen_log", None):
                    ident = t.session_id or self.headers.get("X-SnapLab-Session", "")
                    t.qwen_log.write("SnapCode", ident, tg["model"], capture.reply)
                with t._active_lock:
                    t._active.discard(upstream)
        finally:
            slot.__exit__(None, None, None)


class _BoundedHTTPServer(ThreadingHTTPServer):
    """Bound connections before ThreadingMixIn allocates handler threads."""
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        self.capacity = threading.BoundedSemaphore(16)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self.capacity.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.capacity.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.capacity.release()


class Translator:
    """Runs the translator HTTP server in a background thread of the SnapLab process."""

    def __init__(self, *, port: int, api_cfg: dict, slots, key_fn, token: str, log_path: Path, redactor=None,
                 on_usage=None, resolve=None, web_mcp=None, restore_paths=True, image_history_limit=0, max_calls=0, output_limit=0, effort=None, egress=None, qwen_log=None, session_id=""):
        self.qwen_log, self.session_id = qwen_log, session_id
        self.max_calls, self.output_limit, self.effort = max_calls, output_limit, effort
        self.ready = None
        self.calls = 0
        self.incomplete = False
        self.length_count = 0
        self.call_limit_reached = False
        self.checkpoint_required = False
        self.uncertain = False
        self.last_error = ""            # the latest model-server failure, for the session's result
        self._requests = set()
        self._request_lock = threading.Lock()
        self.port = int(port)
        self.base_url = api_cfg["base_url"].rstrip("/")
        self.model = api_cfg["model"]
        self.key_env = api_cfg.get("api_key_env") or "SNAPSCRIBE_API_KEY"
        self.timeout = float(api_cfg.get("timeout_s", 600))
        self.user_agent = api_cfg.get("user_agent") or "snaplab/0.1"
        self.slots = slots
        self.key = key_fn
        self.token = token
        self.redactor = redactor
        self.log_path = Path(log_path)
        self.on_usage = on_usage
        self.resolve = resolve        # model name or id -> target dict (several providers); None = the single api_cfg
        self.web_mcp = web_mcp
        self.restore_paths = restore_paths
        self.image_history_limit = image_history_limit
        self.egress = egress
        self._lock = threading.Lock()
        self.cancelled = threading.Event()
        self._active_lock = threading.Lock()
        self._active = set()
        self.server = None
        self.error = None

    def target(self, model_ref: str | None) -> dict:
        if self.resolve:
            return self.resolve(model_ref)
        return {"name": self.model, "model": self.model, "base_url": self.base_url, "key": self.key(), "api_key_env": self.key_env,
                "user_agent": self.user_agent, "timeout_s": self.timeout, "effort_style": "chat_template_kwargs",
                "extra": {}, "slots": self.slots}

    def log(self, rec: dict) -> None:
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **rec}
        from .guard import Guard
        rec = Guard(values=[self.token]).clean_obj(rec)
        if self.redactor:
            rec = self.redactor.guard.clean_obj(rec)
        with self._lock:
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except OSError:
                pass

    def usage(self, tin: int, tout: int, ok: bool, model: str | None = None) -> None:
        if self.on_usage:
            try:
                self.on_usage(input_tokens=tin, output_tokens=tout, ok=ok, source="codex", model=model, caller=self.session_id)
            except Exception:  # noqa: BLE001
                pass

    def start(self) -> bool:
        handler = type("TranslatorHandler", (_Handler,), {"owner": self})
        try:
            self.server = _BoundedHTTPServer(("127.0.0.1", self.port), handler)
        except OSError as e:
            self.error = f"port {self.port} is in use ({e})"
            return False
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True,
                         name="translator").start()
        return True

    def stop(self) -> None:
        self.cancelled.set()
        with self._active_lock:
            active = list(self._active)
        for upstream in active:
            try:
                # Interrupt a blocked urllib stream without waiting for its read lock.
                import socket
                upstream.fp.raw._sock.shutdown(socket.SHUT_RDWR)
            except (AttributeError, OSError):
                pass
        if self.server:
            self.server.shutdown()
            self.server.server_close()
