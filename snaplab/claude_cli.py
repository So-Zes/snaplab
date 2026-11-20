"""Claude Code CLI as a bot's model: the bot's whole turn runs in `claude -p` on this PC, with the user's own login.

Added in Settings > Advanced (cli_admin.py), or in config.json:

    "providers": {"claude": {"kind": "claude_cli", "label": "Claude Code",
                             "exe": null,              # optional path to claude(.exe); found on PATH otherwise
                             "timeout_s": 0}},         # 0 = no limit (Stop still ends the turn)
    "models": {"claude": {"provider": "claude", "model": "opus", "label": "Claude (Opus)"}}

Then pick that model for a bot. SnapLab still builds the bot's context (prompt, memory, history) as for any
model. Claude runs its own tool loop, so SnapLab offers the bot's usual tools (memory, dashboard, sessions,
web, plugins and MCP servers added in Settings) through a loopback MCP endpoint that lives for one turn and
is guarded by a random bearer token. Every call goes through the same Toolbox, so chips, approval cards,
untrusted-material marking and the archive behave exactly as for other bots.

Isolation: Claude Code's built-in tools are off (`--tools ""`): a chat bot never runs commands or edits files,
as for every SnapLab bot; code work still goes through sessions. Only the SnapLab MCP server is loaded
(`--strict-mcp-config`), so MCP servers configured for the user's own Claude Code are not reachable from chat.
No settings files, hooks or CLAUDE.md are loaded and nothing is saved as a Claude Code session.

SnapLab runs the unmodified `claude` program the user installed and signed in to through Anthropic's own flow; it
never reads, stores or forwards the login.
"""
from __future__ import annotations

import hmac
import json
import os
import queue
import re
import secrets
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .agent import LoopResult
from .llm import ApiError, Cancelled, Reply

EFFORTS = ("low", "medium", "high", "xhigh", "max")
SERVER = "snaplab"
VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
MAX_REQUEST = 4 * 1024 * 1024
TOOL_NOTE = """

# Tools in this app
Your tools come from the SnapLab MCP server. Wherever these instructions name a tool such as `write_memory`,
call `mcp__snaplab__write_memory`. You have no shell, file or browser tools of your own here.
When a tool result says the user must approve or answer first, end your reply and wait."""


STATUS_TTL_S = 60
_status_cache: dict = {}


def search_path() -> str:
    """PATH plus the folders an installer may have added since SnapLab started (Windows keeps them in the registry)."""
    parts = [os.environ.get("PATH") or ""]
    if os.name == "nt":
        try:
            import winreg
            for root, sub in ((winreg.HKEY_CURRENT_USER, r"Environment"),
                              (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
                try:
                    with winreg.OpenKey(root, sub) as k:
                        parts.append(os.path.expandvars(str(winreg.QueryValueEx(k, "Path")[0])))
                except OSError:
                    pass
        except Exception:  # noqa: BLE001 - no registry (or a test's stand-in): PATH alone
            pass
    return os.pathsep.join(p for p in parts if p)


def which(name: str, extra: list[Path]) -> str | None:
    """A program on PATH (as it is now) or in one of the usual install folders."""
    found = shutil.which(name, path=search_path())
    if found:
        return found
    for folder in extra:
        for candidate in ([folder / (name + ".exe"), folder / (name + ".cmd")] if os.name == "nt" else [folder / name]):
            if candidate.is_file():
                return str(candidate)
    return None


def probe(args: list[str], timeout: float = 20) -> tuple[int | None, str]:
    """Run a quick status command (no window) and return (exit code, output)."""
    try:
        r = subprocess.run(args, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired):
        return None, ""


def cached(key: str, fresh: bool, make) -> dict:
    hit = _status_cache.get(key)
    if hit and not fresh and time.monotonic() - hit[0] < STATUS_TTL_S:
        return hit[1]
    value = make()
    _status_cache[key] = (time.monotonic(), value)
    return value


def status(configured: str | None = None, fresh: bool = False) -> dict:
    """Installed, version, signed in, plan. Never the account's email or organization."""
    def make():
        exe = find_exe(configured)
        out = {"installed": bool(exe), "version": None, "signed_in": False, "plan": None}
        if exe:
            _, text = probe([exe, "--version"])
            m = re.search(r"\d+\.\d+\.\d+", text)
            out["version"] = m.group(0) if m else None
            code, text = probe([exe, "auth", "status"])
            try:
                info = json.loads(text[text.find("{"):]) if "{" in text else {}
            except ValueError:
                info = {}
            out["signed_in"] = code == 0 and info.get("loggedIn") is not False
            plan = info.get("subscriptionType") or info.get("authMethod")
            out["plan"] = str(plan)[:40] if out["signed_in"] and plan else None
        return out
    return cached("claude:" + (configured or ""), fresh, make)


def find_exe(configured: str | None = None) -> str | None:
    """The real claude executable (the npm .cmd wrapper cannot be stopped cleanly, so look behind it)."""
    if configured:
        return configured if Path(configured).exists() else None
    home = Path.home()
    found = which("claude", [home / ".local" / "bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin")])
    if not found:
        return None
    p = Path(found)
    if p.suffix.lower() in (".cmd", ".ps1", "") and os.name == "nt":
        exe = p.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        if exe.exists():
            return str(exe)
        alt = shutil.which("claude.exe")
        if alt:
            return alt
    return str(p)


# ---------------------------------------------------------------- the loopback MCP endpoint

class _Turn:
    def __init__(self, token, tools, execute, cancel):
        self.token, self.tools, self.execute, self.cancel = token, tools, execute, cancel
        self.lock = threading.Lock()         # Toolbox is used by one turn at a time; Claude may call in parallel
        self.stopped = False
        self.called: list[str] = []
        self.counter = 0


class ToolBridge:
    """Serves each running Claude turn's tools at http://127.0.0.1:<port>/t/<turn>. Started on first use."""

    def __init__(self):
        self.turns: dict[str, _Turn] = {}
        self.lock = threading.Lock()
        self.server = None
        self.port = None

    def _start(self):
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, code, body=None, headers=None):
                data = json.dumps(body).encode() if body is not None else b""
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                if body is not None:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._send(405, headers={"Allow": "POST"})

            def do_DELETE(self):
                self._send(405, headers={"Allow": "POST"})

            def do_POST(self):
                host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
                m = re.fullmatch(r"/t/([A-Za-z0-9_-]{8,64})", self.path)
                turn = bridge.turns.get(m.group(1)) if m else None
                auth = self.headers.get("Authorization") or ""
                if host not in ("127.0.0.1", "localhost") or self.headers.get("Origin"):
                    return self._send(403)
                if not turn or not hmac.compare_digest(auth.encode(), ("Bearer " + turn.token).encode()):
                    return self._send(404)
                try:
                    size = int(self.headers.get("Content-Length") or 0)
                    if size <= 0 or size > MAX_REQUEST:
                        return self._send(413)
                    msg = json.loads(self.rfile.read(size))
                except (ValueError, OSError):
                    return self._send(400, {"jsonrpc": "2.0", "id": None,
                                            "error": {"code": -32700, "message": "parse error"}})
                if isinstance(msg, list) or not isinstance(msg, dict):
                    return self._send(400, {"jsonrpc": "2.0", "id": None,
                                            "error": {"code": -32600, "message": "send one request at a time"}})
                if "id" not in msg:
                    return self._send(202)
                try:
                    result = bridge.dispatch(turn, msg.get("method"), msg.get("params") or {})
                    body = {"jsonrpc": "2.0", "id": msg["id"], "result": result}
                except LookupError as e:
                    body = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": str(e)}}
                self._send(200, body)

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                pass        # Claude Code drops its keep-alive connection when the turn ends

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, name="claude-tool-bridge", daemon=True).start()

    def open(self, tools: list[dict], execute, cancel) -> tuple[str, str, _Turn]:
        with self.lock:
            if self.server is None:
                self._start()
            turn_id, token = secrets.token_urlsafe(16), secrets.token_urlsafe(32)
            t = _Turn(token, tools, execute, cancel)
            self.turns[turn_id] = t
        return turn_id, f"http://127.0.0.1:{self.port}/t/{turn_id}", t

    def close(self, turn_id: str) -> None:
        with self.lock:
            self.turns.pop(turn_id, None)

    def dispatch(self, turn: _Turn, method, params) -> dict:
        if method == "initialize":
            asked = params.get("protocolVersion")
            return {"protocolVersion": asked if asked in VERSIONS else VERSIONS[-2],
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER, "version": "0.1"}}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [{"name": t["function"]["name"], "description": t["function"].get("description") or "",
                               "inputSchema": t["function"].get("parameters") or {"type": "object", "properties": {}}}
                              for t in turn.tools]}
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments")
            if not isinstance(args, dict):
                args = {} if args is None else None
            with turn.lock:
                if turn.cancel():
                    return _text("This conversation was stopped. Do not take further actions.", True)
                if turn.stopped:
                    return _text("The turn is waiting for the user. Do not call more tools; end your reply now.", True)
                turn.counter += 1
                turn.called.append(name)
                res = turn.execute(name, args, f"cl{turn.counter}")
                if res.stop:
                    turn.stopped = True
                failed = res.content.startswith("Error")
                out = _text(res.content, failed)
                for image in res.images or []:          # read_attachment shows an image (MCP image content)
                    out["content"].append({"type": "image", "data": image["data"], "mimeType": image["mime"]})
                return out
        raise LookupError(f"unknown method {method}")


def _text(text: str, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": bool(error)}


# ---------------------------------------------------------------- the model client

def _plain(content) -> str:
    if isinstance(content, str):
        return content
    out = []
    for part in content or []:
        if isinstance(part, dict) and part.get("type") == "text":
            out.append(str(part.get("text") or ""))
        elif isinstance(part, dict) and part.get("type") == "image_url":
            out.append("[image]")
    return "\n".join(out)


def _blocks(content) -> list[dict]:
    """The latest message as Anthropic content blocks (data-URL images become base64 image blocks)."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    blocks = []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            blocks.append({"type": "text", "text": str(part.get("text") or "")})
        elif part.get("type") == "image_url":
            url = (part.get("image_url") or {}).get("url") if isinstance(part.get("image_url"), dict) else part.get("image_url")
            m = re.match(r"data:(image/[a-z+]+);base64,(.+)", str(url or ""), re.S)
            if m:
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": m.group(1), "data": m.group(2)}})
            else:
                blocks.append({"type": "text", "text": "[image]"})
    return blocks or [{"type": "text", "text": "(empty message)"}]


def split_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    """SnapLab chat messages -> (system prompt, content blocks of one user message)."""
    system = "\n\n".join(_plain(m.get("content")) for m in messages if m.get("role") == "system")
    rest = [m for m in messages if m.get("role") != "system"]
    if not rest:
        return system, [{"type": "text", "text": "Continue."}]
    *history, last = rest
    blocks = []
    if history:
        lines, images = [], []
        for m in history:
            who = {"user": "user", "assistant": "you", "tool": "tool result"}.get(m.get("role"), m.get("role"))
            text = _plain(m.get("content")).strip()
            for b in _blocks(m.get("content")) if isinstance(m.get("content"), list) else []:
                if b["type"] == "image":    # images earlier in the conversation (photos the user sent) follow it
                    images.append(b)
                    text = text.replace("[image]", f"[image {len(images)}, shown after this conversation]", 1)
            if text:
                lines.append(f"[{who}]\n{text}")
        blocks.append({"type": "text", "text": "<conversation_so_far>\n" + "\n\n".join(lines) +
                       "\n</conversation_so_far>" + ("" if images else "\n\nThe newest message follows. Reply to it.")})
        for i, b in enumerate(images, 1):
            blocks += [{"type": "text", "text": f"[image {i}]"}, b]
        if images:
            blocks.append({"type": "text", "text": "The newest message follows. Reply to it."})
    if last.get("role") == "user":
        blocks += _blocks(last.get("content"))
    else:
        blocks.append({"type": "text", "text": _plain(last.get("content")) + "\n\n[SnapLab note] Continue."})
    return system, blocks


class ClaudeCLI:
    """Drop-in for llm.LLM where SnapLab expects a model client; `agentic` turns go through run_agent."""

    agentic = True
    is_real = True

    def __init__(self, spec: dict, provider: dict, data_dir: str, bridge: ToolBridge, usage=None, redactor=None):
        self.name = spec["name"]
        self.model = spec["model"]
        self.provider = provider
        self.exe_setting = provider.get("exe")
        self.timeout_s = float(provider.get("timeout_s") or 0)       # 0 = no limit
        self.run_dir = Path(data_dir) / "claude"
        self.bridge = bridge
        self.usage = usage
        self.redactor = redactor
        self.qwen_log = None

    # The registry asks these of every client.
    def key(self):
        return None

    def has_key(self) -> bool:
        """Installed and signed in (checked at most once a minute)."""
        return status(self.exe_setting)["signed_in"]

    # ------------------------------------------------------------ one process

    def _command(self, exe, system_file, mcp_file, effort, with_tools) -> list[str]:
        cmd = [exe, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
               "--include-partial-messages", "--no-session-persistence", "--setting-sources", "",
               "--strict-mcp-config", "--tools", "", "--system-prompt-file", str(system_file)]
        if self.model:
            cmd += ["--model", self.model]
        if effort in EFFORTS:
            cmd += ["--effort", effort]
        if with_tools:
            cmd += ["--mcp-config", str(mcp_file), "--allowedTools", f"mcp__{SERVER}", "--permission-mode", "dontAsk"]
        return cmd

    def _env(self) -> dict:
        env = dict(os.environ)
        # Keys SnapLab holds stay with SnapLab (Claude Code has its own login and no shell here anyway).
        for k in list(env):
            if k.endswith(("_API_KEY", "_TOKEN")) and not k.startswith(("ANTHROPIC_", "CLAUDE_")):
                env.pop(k, None)
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        return env

    def _run(self, system: str, blocks: list[dict], *, tools=None, execute=None, effort=None, cancel=None,
             on_reasoning=None, on_content=None, on_step=None, on_wait=None, source="chat"):
        exe = find_exe(self.exe_setting)
        if not exe:
            raise ApiError(None, "Claude Code was not found on this PC",
                           "install Claude Code and sign in (Settings > Advanced), or set providers.<name>.exe")
        cancel = cancel or (lambda: False)
        run_id = secrets.token_hex(6)
        work = self.run_dir / "work"
        work.mkdir(parents=True, exist_ok=True)
        tmp = self.run_dir / "run"
        tmp.mkdir(parents=True, exist_ok=True)
        system_file, mcp_file = tmp / f"{run_id}.system.md", tmp / f"{run_id}.mcp.json"
        turn_id = turn = None
        if tools:
            turn_id, url, turn = self.bridge.open(tools, execute, cancel)
        if self.redactor:
            system = self.redactor.clean_obj(system)
            blocks = self.redactor.clean_obj(blocks)
        proc = None
        reply = Reply()
        started = time.perf_counter()
        recorded = False
        try:
            system_file.write_text(system + (TOOL_NOTE if tools else ""), encoding="utf-8")
            if tools:
                mcp_file.write_text(json.dumps({"mcpServers": {SERVER: {"type": "http", "url": url,
                    "headers": {"Authorization": "Bearer " + turn.token}}}}), encoding="utf-8")
            if on_wait:
                on_wait(0)
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = subprocess.Popen(self._command(exe, system_file, mcp_file, effort, bool(tools)), cwd=str(work),
                                    env=self._env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, creationflags=flags)
            line = json.dumps({"type": "user", "message": {"role": "user", "content": blocks}}, ensure_ascii=False)
            proc.stdin.write(line.encode("utf-8") + b"\n")
            proc.stdin.close()
            lines: queue.Queue = queue.Queue()
            err: list[bytes] = []
            threading.Thread(target=lambda: [lines.put(x) for x in proc.stdout] + [lines.put(None)], daemon=True).start()
            threading.Thread(target=lambda: err.append(proc.stderr.read()), daemon=True).start()
            result, steps, step_texts, current = None, 0, [], []
            deadline = time.monotonic() + self.timeout_s if self.timeout_s > 0 else None
            while True:
                if cancel():
                    raise Cancelled()
                if deadline is not None and time.monotonic() > deadline:
                    raise ApiError(None, "Claude Code took too long", "the turn passed providers.<name>.timeout_s")
                try:
                    raw = lines.get(timeout=0.2)
                except queue.Empty:
                    continue
                if raw is None:
                    break
                try:
                    ev = json.loads(raw)
                except ValueError:
                    continue
                kind = ev.get("type")
                if kind == "stream_event":
                    e = ev.get("event") or {}
                    if e.get("type") == "message_start":
                        if steps and on_step:
                            on_step(steps)
                        if current:
                            step_texts.append("".join(current))
                            current = []
                        steps += 1
                    elif e.get("type") == "content_block_delta":
                        d = e.get("delta") or {}
                        if d.get("type") == "text_delta" and d.get("text"):
                            current.append(d["text"])
                            reply.content += d["text"]
                            if on_content:
                                on_content(d["text"])
                        elif d.get("type") == "thinking_delta" and d.get("thinking"):
                            reply.reasoning += d["thinking"]
                            if on_reasoning:
                                on_reasoning(d["thinking"])
                elif kind == "result":
                    result = ev
            step_texts.append("".join(current))
            proc.wait(timeout=30)
            reply.elapsed_s = round(time.perf_counter() - started, 2)
            if not result:
                detail = (b"".join(err).decode("utf-8", "replace").strip() or f"exit code {proc.returncode}")[-300:]
                raise ApiError(None, "Claude Code stopped without an answer", detail)
            u = result.get("usage") or {}
            reply.input_tokens = int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0) + \
                int(u.get("cache_creation_input_tokens") or 0)
            reply.output_tokens = int(u.get("output_tokens") or 0)
            if result.get("is_error") or str(result.get("subtype") or "").startswith("error"):
                text = str(result.get("result") or result.get("subtype") or "error")
                hint = "run `claude` in a terminal and sign in" if re.search(r"log ?in|auth", text, re.I) else \
                    ("Claude usage limit reached" if re.search(r"limit", text, re.I) else "")
                self._record(reply, False, source)
                recorded = True
                raise ApiError(None, text[:300], hint)
            if not reply.content.strip() and result.get("result"):
                reply.content = str(result["result"])
                step_texts[-1] = reply.content
            reply.finish_reason = "stop"
            self._record(reply, True, source)
            return reply, steps, [s for s in step_texts], (turn.called if turn else [])
        except ApiError:
            if proc is not None and not recorded:     # timeouts and runs without a result count as failed calls
                self._record(reply, False, source)
            raise
        finally:
            if proc and proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=5)          # Windows keeps files open until the process is gone
                except subprocess.TimeoutExpired:
                    pass
            if turn_id:
                self.bridge.close(turn_id)
            for f in (system_file, mcp_file):
                try:
                    f.unlink()
                except OSError:
                    pass

    def _record(self, r: Reply, ok: bool, source: str) -> None:
        if self.usage:
            self.usage.record(input_tokens=r.input_tokens, output_tokens=r.output_tokens, ok=ok, source=source,
                              model=self.name)

    # ------------------------------------------------------------ the two entry points

    def stream_chat(self, messages, *, tools=None, effort="low", max_tokens=4096, on_reasoning=None,
                    on_content=None, cancel=None, source="chat", kind="chat", on_wait=None, tool_choice="auto") -> Reply:
        """A single answer without tools (summaries, memory tidy, anything that only needs text)."""
        system, blocks = split_messages(messages)
        reply, *_ = self._run(system, blocks, effort=effort, cancel=cancel, on_reasoning=on_reasoning,
                              on_content=on_content, on_wait=on_wait, source=source)
        return reply

    def run_agent(self, messages, tools, execute, *, effort=None, cancel=None, on_reasoning=None, on_content=None,
                  on_step=None, on_wait=None, source="chat", **_ignored) -> LoopResult:
        """A whole bot turn: Claude calls SnapLab's tools through the bridge until it has its answer."""
        system, blocks = split_messages(messages)
        reply, steps, step_texts, called = self._run(system, blocks, tools=tools or None, execute=execute,
                                                     effort=effort, cancel=cancel, on_reasoning=on_reasoning,
                                                     on_content=on_content, on_step=on_step, on_wait=on_wait,
                                                     source=source)
        final = step_texts[-1] if step_texts and step_texts[-1].strip() else reply.content
        return LoopResult(text=final, stop_reason="done", steps=max(steps, 1), input_tokens=reply.input_tokens,
                          output_tokens=reply.output_tokens, calls=1, first_input_tokens=reply.input_tokens,
                          step_texts=step_texts, tools_called=called)
