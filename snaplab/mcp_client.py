"""External MCP servers as bot tools: list a server in config.json and its tools appear next to the plugins.

config.json:

    "mcp_servers": {
      "notes": {"url": "http://127.0.0.1:8765/mcp",
                "approvals": {"delete_note": "always"},                         # per tool (optional)
                "about": "The user's notes",                                     # what bots are told it is for
                                                                                 # (optional; else the server's own)
                "exclude": ["delete_note"],                                      # tools bots never get (optional)
                "bearer_env": "NOTES_TOKEN",                                     # optional Authorization header
                 "untrusted": true,                                              # results are outside material
                 "chat": true,                                                   # false: SnapCode only (default true)
                 "timeout_s": 0}                                                 # 0 (default) = no time limit
    }

Chat bots and SnapCode (sessions: Codex, Pi and "bot" sessions) both get the tools. A chat bot can still hand big
jobs to SnapCode to keep its own context small, for example when the user asks for that. "chat": false keeps a server
for SnapCode only; chat bots are then just told it exists. Sessions run unattended, so a tool that config sets to
"always" (a proposal card) is not offered there.

Two kinds of servers: Streamable HTTP ("url", JSON or SSE responses) and stdio ("command" and "args", the form most
MCP servers document for Claude Code, Codex and other clients). A stdio server runs on this PC as the user, started on
first use and kept running; servers added in Settings live in <data>/mcp_servers.json (mcp_admin.py) with their
secret environment values in the OS credential store. Each tool becomes a bot tool named
`<server>_<tool>`. Every tool runs directly by default, also the ones the server marks destructiveHint (until
2026-11-21 those waited for a card). A per-tool entry, or the global "approvals" block, can ask for a card. Results
are wrapped as untrusted material by default. A call waits without a time limit unless timeout_s is set; stopping
the turn ends the wait.

The tool list is cached in <data>/mcp_tools.json, so a server that is down when SnapLab starts still has its
tools; calling them while it is down returns an error to the bot instead of breaking the turn.
"""
from __future__ import annotations

import collections
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .plugins import NAME_RE, APPROVALS, PluginTool
from .provider_protocol import RequestCancelled, open_request

PROTOCOL = "2025-06-18"
SERVER_RE = re.compile(r"^[a-z][a-z0-9]{1,15}$")
MAX_RESULT_CHARS = 2_000_000     # memory safety only; the agent loop fits results to the model's context window


class MCPError(ValueError):
    pass


def _sse_messages(text: str):
    data = []
    for line in text.splitlines():
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
        elif not line.strip() and data:
            yield "\n".join(data)
            data = []
    if data:
        yield "\n".join(data)


class _Tools:
    """tools/list and tools/call on top of call(method, params); shared by both transports."""

    def list_tools(self) -> list[dict]:
        tools, cursor = [], None
        for _ in range(20):
            result = self.call("tools/list", {"cursor": cursor} if cursor else {})
            tools += [t for t in result.get("tools") or [] if isinstance(t, dict) and t.get("name")]
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, tool: str, args: dict, cancel=None) -> tuple[str, bool]:
        result = self.call("tools/call", {"name": tool, "arguments": args}, cancel=cancel)
        parts = []
        for item in result.get("content") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            elif item.get("type") == "resource" and isinstance(item.get("resource"), dict):
                parts.append(str(item["resource"].get("text") or ""))
            else:
                parts.append(f"[{item.get('type')} content omitted]")
        if not parts and result.get("structuredContent") is not None:
            parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
        text = "\n".join(parts).strip()
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + f"\n…[{len(text) - MAX_RESULT_CHARS} characters cut]"
        return text, bool(result.get("isError"))

    def close(self) -> None:
        pass


class MCPClient(_Tools):
    """A small Streamable HTTP client: initialize once, then tools/list and tools/call."""

    def __init__(self, name: str, url: str, *, token: str | None = None, timeout_s: float | None = 0):
        if not str(url).startswith(("http://", "https://")):
            raise MCPError(f"MCP server {name!r} needs an http(s) url")
        from .config import wait_s
        self.name, self.url, self.token, self.timeout_s = name, url, token, wait_s(timeout_s)   # None = no limit
        self.session_id: str | None = None
        self.ready = False
        self.instructions = ""
        self._id = 0
        self._lock = threading.Lock()

    def _post(self, payload: dict, expect_reply: bool = True, cancel=None):
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": PROTOCOL}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        try:
            with open_request(req, timeout=self.timeout_s, cancel=cancel) as resp:
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self.session_id = sid
                body = resp.read().decode("utf-8", "replace")
                ctype = resp.headers.get("Content-Type") or ""
        except urllib.error.HTTPError as e:
            if e.code == 404 and self.session_id:
                # The server forgot our session (it restarted). Start again once.
                self.session_id, self.ready = None, False
                raise _Expired() from None
            raise MCPError(f"{self.name} answered HTTP {e.code}") from None
        except RequestCancelled:
            raise MCPError(f"the call to {self.name} was stopped") from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            if cancel and cancel():
                raise MCPError(f"the call to {self.name} was stopped") from None
            raise MCPError(f"{self.name} is not reachable ({getattr(e, 'reason', e)})") from None
        if not expect_reply:
            return None
        candidates = list(_sse_messages(body)) if "text/event-stream" in ctype else [body]
        for raw in candidates:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("id") == payload.get("id"):
                if msg.get("error"):
                    raise MCPError(f"{self.name}: {(msg['error'] or {}).get('message') or 'error'}")
                return msg.get("result") or {}
        raise MCPError(f"{self.name} sent no answer")

    def _request(self, method: str, params: dict | None = None, cancel=None) -> dict:
        self._id += 1
        return self._post({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}, cancel=cancel)

    def _initialize(self) -> None:
        result = self._request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                              "clientInfo": {"name": "snaplab", "version": "0.1"}})
        self.instructions = str(result.get("instructions") or "")[:2000]
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect_reply=False)
        except MCPError:
            pass        # stateless servers may refuse notifications; tools still work
        self.ready = True

    def call(self, method: str, params: dict | None = None, cancel=None) -> dict:
        with self._lock:
            for attempt in (0, 1):
                try:
                    if not self.ready:
                        self._initialize()
                    return self._request(method, params, cancel=cancel)
                except _Expired:
                    if attempt:
                        raise MCPError(f"{self.name} keeps dropping the session") from None
            raise MCPError(f"{self.name} keeps dropping the session")


class StdioMCPClient(_Tools):
    """An MCP server started as a program on this PC, talking newline-delimited JSON-RPC on stdin/stdout.

    Started on first use and kept running; started again when it has exited. Requests from the server (ping,
    roots, sampling) get a ping reply or "not supported"."""

    def __init__(self, name: str, command: str, args=(), *, env: dict | None = None, cwd: str | None = None,
                 timeout_s: float | None = 0):
        if not command:
            raise MCPError(f"MCP server {name!r} needs a command")
        from .config import wait_s
        self.name, self.command, self.args = name, str(command), [str(a) for a in args or []]
        self.env, self.cwd, self.timeout_s = dict(env or {}), cwd, wait_s(timeout_s)        # None = no limit
        self.proc: subprocess.Popen | None = None
        self.ready = False
        self.instructions = ""
        self.stderr_tail: collections.deque = collections.deque(maxlen=12)
        self._id = 0
        self._lock = threading.Lock()
        self._cond = threading.Condition()
        self._replies: dict = {}
        self._write_lock = threading.Lock()

    def _start(self) -> None:
        exe = shutil.which(self.command) or self.command
        flags = 0x08000000 if os.name == "nt" else 0          # no console window
        try:
            self.proc = subprocess.Popen([exe, *self.args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, env={**os.environ, **self.env}, cwd=self.cwd or None,
                                         creationflags=flags, start_new_session=os.name != "nt")
        except OSError as e:
            raise MCPError(f"{self.name} could not start ({e.strerror or e})") from None
        self.ready = False
        threading.Thread(target=self._read_out, args=(self.proc,), daemon=True, name=f"mcp-{self.name}").start()
        threading.Thread(target=self._read_err, args=(self.proc,), daemon=True).start()

    def _read_out(self, proc) -> None:
        for raw in iter(proc.stdout.readline, b""):
            try:
                msg = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            if "method" in msg and "id" in msg:
                reply = {"jsonrpc": "2.0", "id": msg["id"]}
                if msg["method"] == "ping":
                    reply["result"] = {}
                else:
                    reply["error"] = {"code": -32601, "message": "not supported by SnapLab"}
                try:
                    self._write(proc, reply)
                except OSError:
                    pass
            elif "id" in msg:
                with self._cond:
                    self._replies[msg["id"]] = msg
                    self._cond.notify_all()
        with self._cond:
            self._cond.notify_all()

    def _read_err(self, proc) -> None:
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if line:
                self.stderr_tail.append(line[:300])

    def _write(self, proc, obj: dict) -> None:
        with self._write_lock:
            proc.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
            proc.stdin.flush()

    def _gone(self) -> str:
        tail = " | ".join(list(self.stderr_tail)[-3:])
        return f"{self.name} stopped" + (f": {tail}" if tail else "")

    def _send(self, payload: dict, expect_reply: bool = True, cancel=None):
        proc = self.proc
        try:
            self._write(proc, payload)
        except (OSError, ValueError):
            raise MCPError(self._gone()) from None
        if not expect_reply:
            return None
        # No time limit unless timeout_s is set; a stopped turn (cancel) stops waiting.
        deadline = time.monotonic() + self.timeout_s if self.timeout_s else None
        with self._cond:
            while payload["id"] not in self._replies:
                if proc.poll() is not None:
                    raise MCPError(self._gone())
                if cancel and cancel():
                    raise MCPError(f"the call to {self.name} was stopped")
                left = deadline - time.monotonic() if deadline is not None else 1.0
                if left <= 0:
                    raise MCPError(f"{self.name} did not answer within {int(self.timeout_s)} s")
                self._cond.wait(min(left, 1.0))
            msg = self._replies.pop(payload["id"])
        if msg.get("error"):
            raise MCPError(f"{self.name}: {(msg['error'] or {}).get('message') or 'error'}")
        return msg.get("result") or {}

    def call(self, method: str, params: dict | None = None, cancel=None) -> dict:
        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                self._start()
            if not self.ready:
                self._id += 1
                result = self._send({"jsonrpc": "2.0", "id": self._id, "method": "initialize",
                                     "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                                                "clientInfo": {"name": "snaplab", "version": "0.2"}}})
                self.instructions = str(result.get("instructions") or "")[:2000]
                self._send({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect_reply=False)
                self.ready = True
            self._id += 1
            return self._send({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}},
                              cancel=cancel)

    def close(self) -> None:
        proc, self.proc = self.proc, None
        if not proc:
            return
        if proc.poll() is not None:
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    pipe.close()
                except OSError:
                    pass
            return
        try:
            if os.name == "nt":            # npx and similar start a child process: end the whole tree
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=20,
                               creationflags=0x08000000)
            else:
                os.killpg(proc.pid, 15)
        except (OSError, subprocess.SubprocessError):
            proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            try:
                pipe.close()
            except OSError:
                pass


class _Expired(Exception):
    pass


def tool_name(server: str, tool: str) -> str:
    base = re.sub(r"[^a-z0-9_]+", "_", str(tool).lower()).strip("_")
    return f"{server}_{base}"[:41]


def default_approval(spec: dict) -> str:
    """Bots use every MCP tool freely, also the ones the server marks destructive; config can ask for a card."""
    return "never"


def _schema(spec: dict) -> dict:
    params = spec.get("inputSchema") or {}
    if not isinstance(params, dict) or params.get("type") != "object":
        params = {"type": "object", "properties": {}}
    params = {k: v for k, v in params.items() if k not in ("$schema",)}
    params.setdefault("properties", {})
    return params


def make_tools(server: str, client: MCPClient, specs: list[dict], conf: dict, reserved: set[str],
               log=print) -> dict[str, PluginTool]:
    out: dict[str, PluginTool] = {}
    exclude = set(conf.get("exclude") or [])
    per_tool = conf.get("approvals") or {}
    untrusted = conf.get("untrusted", True) is not False
    title = conf.get("label") or server
    for spec in specs:
        raw = spec.get("name")
        if raw in exclude:
            continue
        name = tool_name(server, raw)
        if not NAME_RE.match(name) or name in reserved or name in out:
            log(f"MCP tool {server}/{raw} skipped: the name {name!r} is invalid or already used")
            continue
        approval = per_tool.get(raw) or default_approval(spec)
        if approval not in APPROVALS:
            log(f"MCP tool {server}/{raw}: approval must be one of {APPROVALS}; using 'always'")
            approval = "always"
        desc = (f"[{title}] " + str(spec.get("description") or spec.get("title") or raw)).strip()[:1000]

        def run(ctx, _raw=raw, **args):
            text, failed = client.call_tool(_raw, args, cancel=getattr(ctx, "cancel", None))
            if failed:
                raise ValueError(text or f"{server} reported an error")
            return text or "(empty result)"

        short = str(raw).replace("_", " ")
        out[name] = PluginTool(name=name, description=desc, parameters=_schema(spec), run=run,
                               scope=("session",) if conf.get("chat") is False else ("chat", "session"), approval=approval,
                               label={"en": f"{title}: {short}"[:40], "ja": f"{title}: {short}"[:40]},
                               untrusted=untrusted, path=f"mcp:{server}")
        out[name].mcp = {"server": server, "label": title, "annotations": dict(spec.get("annotations") or {}),
                         "about": str(conf.get("about") or client.instructions or "").strip()}
    return out


def servers(plugin_tools: dict) -> dict[str, dict]:
    """Connected MCP servers among the bot's tools: {server: {label, about, tools: [names]}}."""
    out: dict[str, dict] = {}
    for name, t in (plugin_tools or {}).items():
        info = getattr(t, "mcp", None)
        if info:
            out.setdefault(info["server"], {"label": info["label"], "about": info["about"], "tools": []})["tools"].append(name)
    return out


def unattended_ok(app, tool) -> bool:
    """Sessions run unattended: a tool that config sets to "always" (a proposal card) is not offered there instead
    of running unasked. Destructive tools are offered like the others (2026-11-21)."""
    return ((app.cfg.get("approvals") or {}).get(tool.name) or tool.approval) != "always"


def session_tools(app) -> dict:
    """External MCP tools that SnapCode may use."""
    return {n: t for n, t in (getattr(app, "plugin_tools", None) or {}).items()
            if getattr(t, "mcp", None) and "session" in t.scope and unattended_ok(app, t)}


def chat_note(plugin_tools: dict) -> str:
    """For chat bots without the tools: which services SnapCode can use, in a few lines."""
    found = {s: i for s, i in servers(plugin_tools).items()
             if not any("chat" in plugin_tools[n].scope for n in i["tools"])}
    if not found:
        return ""
    lines = ["# Services SnapCode can use",
             "SnapLab is connected to these services. Their tools run in SnapCode sessions, not in this chat, so their "
             "raw output never fills your context. To get data from them or act on them, start a session "
             "(or continue the related one with send_to_session) and say which service to use and what to report "
             "back; then answer from the session's result. Never claim a result no session has reported."]
    for server, info in found.items():
        about = " ".join(info["about"].split())
        about = (about[:160] + "…") if len(about) > 160 else about
        lines.append(f"- {server} ({info['label']})" + (f": {about}" if about else ""))
    return "\n".join(lines)


def prompt_block(plugin_tools: dict) -> str:
    """What the bot is told about its MCP connections, so it neither denies nor invents them."""
    found = servers(plugin_tools)
    if not found:
        return ""
    lines = ["# Connected services (MCP)",
             "SnapLab is connected to these services for you right now. Their tools are in your function list; "
             "call them by these exact names. If you are unsure whether one works, call one of its read tools "
             "(such as list_accounts or get_status) instead of guessing. Never say you checked, fetched or changed "
             "something in one of these services unless you called its tool in this turn. SnapCode sessions have "
             "the same tools: when the user asks you to keep this chat light, or a job would pull in a lot of data, "
             "start a session (or continue the related one) to do it and answer from its result."]
    for server, info in found.items():
        about = " ".join(info["about"].split())
        # 240 cut a typical server's own instructions mid-sentence (measured: one server's per-user lists, ~330 characters).
        about = (about[:480] + "…") if len(about) > 480 else about
        lines.append(f"- {server} ({info['label']})" + (f": {about}" if about else ""))
        lines.append("  tools: " + ", ".join(info["tools"]))
    return "\n".join(lines)


# One sentence that names the service and says an action finished, or that it is missing.
_DONE_JA = re.compile(r"(確認|取得|調べ|チェック|検索|見|読|追加|登録|拾)[^。\n]{0,6}?"
                      r"(ました|たところ|た結果|ておきました|済み|た(?=[。、！!？?\s]|$))")
_DONE_EN = re.compile(r"\b(checked|fetched|pulled|looked (it )?up|searched|added|found)\b", re.I)
_DENY = re.compile(r"接続されていな|接続されていません|繋がっていな|繋がっていません|つながっていな|つながっていません|"
                   r"来ていません|ありません|使えません|\bnot connected\b|\bisn'?t connected\b|\bno (such )?tools?\b|"
                   r"\bdon'?t have\b|\bdo not have\b", re.I)
_DENY_CONNECTION = re.compile(r"接続されていな|接続されていません|繋がっていな|繋がっていません|つながっていな|"
                              r"つながっていません|使えません|使えない|ツール(は|が)?(ありません|ない|来ていません)|"
                              r"\bnot connected\b|\bisn'?t connected\b|\bno access\b|\bno (such )?tools?\b", re.I)
_SENTENCES = re.compile(r"[^。.!?！？\n]+")


class SentenceRule:
    """search(text) matches when a sentence names one of the terms and claims a finished action
    (unless deny_only) or a missing connection."""

    def __init__(self, terms: list[str], deny_only: bool = False, deny=None):
        self.deny = deny or _DENY
        self.term = re.compile("|".join(r"(?<![A-Za-z0-9])" + re.escape(t) + r"(?![A-Za-z0-9])" for t in terms), re.I)
        self.deny_only = deny_only

    def search(self, text: str):
        for m in _SENTENCES.finditer(text or ""):
            s = m.group(0)
            if not self.term.search(s):
                continue
            if self.deny.search(s) or (not self.deny_only and (_DONE_JA.search(s) or _DONE_EN.search(s))):
                return m
        return None


def claim_rules(plugin_tools: dict) -> list[tuple]:
    """(tools, which to call, what the reply claims, rule) for agent.claim_gaps.

    A reply that says it checked or fetched something in a named service, or that the service or MCP is not
    connected, without calling one of its tools in that turn, is sent back once to call it or correct itself."""
    found = servers(plugin_tools)
    if not found:
        return []
    terms: dict[str, set] = {}
    for server, info in found.items():
        words = {server.lower()} | {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", info["label"])}
        for w in words:
            terms.setdefault(w, set()).update(info["tools"])
    rules = []
    for term, tools in sorted(terms.items()):
        # The rule only applies when the bot has these tools (agent.claim_gaps checks the overlap).
        example = (sorted(t for t in tools if t.startswith(term + "_")) or sorted(tools))[0]
        rules.append((tools, f"one of the {term} tools (for example {example})",
                      f"something about {term} (that you checked or fetched it there, or that it is not available)",
                      SentenceRule([term])))
    every = set().union(*(set(i["tools"]) for i in found.values()))
    rules.append((every, "one of the connected MCP tools listed under Connected services",
                  "that an MCP connection is missing or not working", SentenceRule(["MCP", "ＭＣＰ"], deny_only=True, deny=_DENY_CONNECTION)))
    # A chat bot without the tools: denying the service is answered with "SnapCode can use it".
    rules.append(({"start_session", "send_to_session"}, "start_session or send_to_session (SnapCode can use the service)",
                  "that a connected service or MCP is not available",
                  SentenceRule(sorted(terms) + ["MCP", "ＭＣＰ"], deny_only=True, deny=_DENY_CONNECTION)))
    return rules


def _cache_path(cfg: dict) -> Path:
    return Path(cfg["data_dir"]) / "mcp_tools.json"


def make_client(server: str, conf: dict, secret=None):
    """The client for one configured server. secret(target) reads a value from the OS credential store."""
    env = {k: str(v) for k, v in (conf.get("env") or {}).items()}
    for key, target in (conf.get("env_secrets") or {}).items():
        value = secret(target) if secret else None
        if value:
            env[key] = value
    if conf.get("command"):
        return StdioMCPClient(server, conf["command"], conf.get("args") or [], env=env, cwd=conf.get("cwd"),
                              timeout_s=conf.get("timeout_s", 0))
    token = os.environ.get(conf["bearer_env"]) if conf.get("bearer_env") else None
    if conf.get("bearer_secret") and secret:
        token = secret(conf["bearer_secret"]) or token
    return MCPClient(server, conf.get("url") or "", token=token, timeout_s=conf.get("timeout_s", 0))


def load(cfg: dict, reserved: set[str], log=print, clients: dict | None = None, opened: dict | None = None,
         secret=None, errors: dict | None = None) -> dict[str, PluginTool]:
    """Connect each configured server and turn its tools into PluginTools (cached list when it is down)."""
    servers = cfg.get("mcp_servers") or {}
    if not servers:
        return {}
    cache_file = _cache_path(cfg)
    try:
        cache = json.loads(cache_file.read_text(encoding="utf-8")) if cache_file.exists() else {}
    except (OSError, ValueError):
        cache = {}
    found: dict[str, PluginTool] = {}
    changed = False
    for server, conf in servers.items():
        if not isinstance(conf, dict) or conf.get("enabled") is False:
            continue
        if not SERVER_RE.match(server):
            log(f"MCP server {server!r} skipped: use 2-16 lowercase letters or digits as its name")
            continue
        try:
            client = (clients or {}).get(server) or make_client(server, conf, secret)
        except MCPError as e:
            log(str(e))
            if errors is not None:
                errors[server] = str(e)
            continue
        if opened is not None:
            opened[server] = client
        normal_timeout = client.timeout_s
        # Listing the tools at start-up: a server that does not answer in time keeps its cached tools for now.
        start = float(conf.get("start_timeout_s", 5.0))
        client.timeout_s = start if normal_timeout is None else min(normal_timeout, start)
        try:
            specs = client.list_tools()
            if cache.get(server) != specs:
                cache[server] = specs
                changed = True
        except MCPError as e:
            specs = cache.get(server) or []
            log(f"MCP server {server}: {e}; " + (f"using {len(specs)} cached tools" if specs else "no tools yet"))
            if errors is not None:
                errors[server] = str(e)
        finally:
            client.timeout_s = normal_timeout
        found.update(make_tools(server, client, specs, conf, reserved | set(found), log=log))
    if changed:
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass
    return found
