"""Codex CLI as a bot's model: the bot's whole turn runs in `codex exec` on this PC (the user's own ChatGPT login).

Added in Settings > Advanced (cli_admin.py), or in config.json:

    "providers": {"codex": {"kind": "codex_cli", "label": "Codex (ChatGPT)",
                            "exe": null,             # optional path to codex(.exe); found on PATH otherwise
                            "timeout_s": 0}},        # 0 = no limit (Stop still ends the turn)
    "models": {"gpt-6.1-terra": {"provider": "codex", "model": "gpt-6.1-terra", "label": "GPT-6.1 Terra"}}

Then pick that model for a bot. As with claude_cli.py, SnapLab builds the bot's
context and Codex runs its own tool loop; SnapLab's tools reach it through the same loopback MCP bridge (one per
turn, guarded by a random bearer token that is passed only in the Codex process environment). Every call goes
through the same Toolbox, so chips, approval cards, untrusted-material marking and the archive behave as usual.

Isolation (measured with Codex CLI 0.162.1): the shell tools are switched off (shell_tool, unified_exec), the
sandbox is read-only with approvals "never" (so apply_patch is refused), and the user's config.toml, rules,
project docs, skills, apps, plugins, browser, computer use and built-in web search are not loaded. Only the
SnapLab MCP server is configured, and its tools need no approval. Runs are --ephemeral, so they never appear in
the user's Codex conversation list. The child process gets a short list of environment variables only.
Features this Codex version does not know are left out of the --disable list (`codex features list`).
"""
from __future__ import annotations

import base64
import json
import os
import queue
import re
import secrets
import subprocess
import threading
import time
from pathlib import Path

from .agent import LoopResult
from .claude_cli import SERVER, cached, probe, split_messages, which
from .llm import ApiError, Cancelled, Reply

TOKEN_ENV = "SNAPLAB_TOOLS_TOKEN"
EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
# Codex features that act on this PC or reach other services by themselves. SnapLab's own tools replace them.
DISABLED = ("shell_tool unified_exec apps plugins remote_plugin multi_agent multi_agent_v2 goals view_image "
            "skill_search tool_suggest browser_use browser_use_external computer_use in_app_browser sleep_tool "
            "hooks workspace_dependencies skill_mcp_dependency_install image_generation memories").split()
ENV_KEEP = {"PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "USERPROFILE", "HOME", "TEMP", "TMP",
            "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "LANG", "LC_ALL", "TZ"}
TOOL_NOTE = """

# Tools in this app
Your tools come from the SnapLab MCP server (`snaplab`). Wherever these instructions name a tool such as
`write_memory`, call the SnapLab tool with that name. You have no shell or file tools of your own here, and
`request_user_input` does not reach the user: SnapLab's `ask_user` does.
When a tool result says the user must approve or answer first, end your reply and wait."""


def find_exe(configured: str | None = None) -> str | None:
    """The real codex executable (the npm .cmd wrapper goes through cmd.exe, which mangles quoted arguments)."""
    if configured:
        return configured if Path(configured).exists() else None
    home = Path.home()
    folders = [home / ".local" / "bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin")]
    if os.environ.get("LOCALAPPDATA"):
        folders.insert(0, Path(os.environ["LOCALAPPDATA"]) / "Programs" / "OpenAI" / "Codex" / "bin")
    found = (which("codex.cmd", []) if os.name == "nt" else None) or which("codex", folders)
    if not found:
        return None
    p = Path(found)
    if os.name == "nt" and p.suffix.lower() in (".cmd", ".ps1", ""):
        pkg = p.resolve().parent / "node_modules" / "@openai" / "codex"
        for c in sorted(pkg.glob("node_modules/@openai/codex-*/vendor/*/bin/codex.exe")):
            return str(c)
    return str(p)


def status(configured: str | None = None, fresh: bool = False) -> dict:
    """Installed, version, signed in, and how (ChatGPT or an API key). Never the account itself."""
    def make():
        exe = find_exe(configured)
        out = {"installed": bool(exe), "version": None, "signed_in": False, "plan": None}
        if exe:
            _, text = probe([exe, "--version"])
            m = re.search(r"\d+\.\d+\.\d+", text)
            out["version"] = m.group(0) if m else None
            code, text = probe([exe, "login", "status"])
            out["signed_in"] = code == 0
            if out["signed_in"]:
                out["plan"] = "ChatGPT" if "chatgpt" in text.lower() else ("API key" if "api key" in text.lower() else None)
        return out
    return cached("codex:" + (configured or ""), fresh, make)


def disabled_features(exe: str) -> list[str]:
    """DISABLED, minus the names this Codex version does not list (an unknown name could stop the run)."""
    def make():
        code, text = probe([exe, "features", "list"])
        names = {line.split()[0] for line in text.splitlines() if line.strip()}
        return {"list": [f for f in DISABLED if f in names] if code == 0 and names else list(DISABLED)}
    return cached("codex-features:" + exe, False, make)["list"]


def child_env(token: str | None = None, exe: str | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k.upper() in ENV_KEEP}
    if exe and "node_modules" in exe:
        env["CODEX_MANAGED_BY_NPM"] = "1"
    if token:
        env[TOKEN_ENV] = token
    return env


def effort_for(effort: str | None) -> str | None:
    if effort in EFFORTS:
        return effort
    return "low" if effort in ("minimal", "none") else None


def prompt_parts(blocks: list[dict], folder: Path) -> tuple[str, list[Path]]:
    """Content blocks -> (prompt text, image files for -i)."""
    texts, images = [], []
    for b in blocks:
        if b.get("type") == "text":
            texts.append(str(b.get("text") or ""))
        elif b.get("type") == "image":
            src = b.get("source") or {}
            ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}.get(
                src.get("media_type"), ".png")
            try:
                data = base64.b64decode(src.get("data") or "", validate=True)
            except ValueError:
                texts.append("[image]")
                continue
            f = folder / f"{secrets.token_hex(6)}{ext}"
            f.write_bytes(data)
            images.append(f)
            texts.append(f"[image {len(images)} attached]")
    return "\n\n".join(t for t in texts if t), images


class CodexCLI:
    """Drop-in for llm.LLM where SnapLab expects a model client; `agentic` turns go through run_agent."""

    agentic = True
    is_real = True

    def __init__(self, spec: dict, provider: dict, data_dir: str, bridge, usage=None, redactor=None):
        self.name = spec["name"]
        self.model = spec["model"]
        self.provider = provider
        self.exe_setting = provider.get("exe")
        self.timeout_s = float(provider.get("timeout_s") or 0)
        self.run_dir = Path(data_dir) / "codex_cli"
        self.bridge = bridge
        self.usage = usage
        self.redactor = redactor
        self.qwen_log = None

    def key(self):
        return None

    def has_key(self) -> bool:
        """Installed and signed in (checked at most once a minute)."""
        return status(self.exe_setting)["signed_in"]

    # ------------------------------------------------------------ one process

    def _command(self, exe, work: Path, instructions: Path, effort, url, images) -> list[str]:
        cmd = [exe, "exec", "--json", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules",
               "-C", str(work), "-s", "read-only", "--color", "never"]
        if self.model:
            cmd += ["-m", self.model]
        values = ['approval_policy="never"', 'web_search="disabled"', "project_doc_max_bytes=0",
                  "model_instructions_file=" + json.dumps(str(instructions)),
                  "skills.bundled.enabled=false", "skills.include_instructions=false",
                  "include_permissions_instructions=false", "include_apps_instructions=false",
                  "include_environment_context=false"]
        if effort_for(effort):
            values.append(f'model_reasoning_effort="{effort_for(effort)}"')
        if url:
            m = f"mcp_servers.{SERVER}."
            values += [m + "url=" + json.dumps(url), m + f'bearer_token_env_var="{TOKEN_ENV}"', m + "required=true",
                       m + "startup_timeout_sec=30", m + "tool_timeout_sec=86400",
                       m + 'default_tools_approval_mode="approve"']
        for v in values:
            cmd += ["-c", v]
        for f in disabled_features(exe):
            cmd += ["--disable", f]
        for img in images:
            cmd += ["-i", str(img)]
        return cmd + ["-"]

    def _run(self, system: str, blocks: list[dict], *, tools=None, execute=None, effort=None, cancel=None,
             on_reasoning=None, on_content=None, on_step=None, on_wait=None, source="chat"):
        exe = find_exe(self.exe_setting)
        if not exe:
            raise ApiError(None, "Codex was not found on this PC",
                           "install the Codex CLI and sign in with ChatGPT (Settings > Advanced), or set providers.<name>.exe")
        cancel = cancel or (lambda: False)
        run_id = secrets.token_hex(6)
        work = self.run_dir / "work"
        work.mkdir(parents=True, exist_ok=True)
        tmp = self.run_dir / "run" / run_id
        tmp.mkdir(parents=True, exist_ok=True)
        instructions = tmp / "instructions.md"
        turn_id = turn = url = None
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
            instructions.write_text(system + (TOOL_NOTE if tools else ""), encoding="utf-8")
            prompt, images = prompt_parts(blocks, tmp)
            if on_wait:
                on_wait(0)
            proc = subprocess.Popen(self._command(exe, work, instructions, effort, url, images), cwd=str(work),
                                    env=child_env(turn.token if turn else None, exe), stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            proc.stdin.write((prompt or "Continue.").encode("utf-8"))
            proc.stdin.close()
            lines: queue.Queue = queue.Queue()
            err: list[bytes] = []
            threading.Thread(target=lambda: [lines.put(x) for x in proc.stdout] + [lines.put(None)], daemon=True).start()
            threading.Thread(target=lambda: err.append(proc.stderr.read()), daemon=True).start()
            done, failure, notes, step_texts, tool_seen = None, None, [], [], False
            deadline = time.monotonic() + self.timeout_s if self.timeout_s > 0 else None
            while True:
                if cancel():
                    raise Cancelled()
                if deadline and time.monotonic() > deadline:
                    raise ApiError(None, "Codex took too long", "the turn passed providers.<name>.timeout_s")
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
                item = ev.get("item") or {}
                if kind == "item.started" and item.get("type") == "mcp_tool_call":
                    tool_seen = True
                elif kind == "item.completed":
                    itype = item.get("type")
                    text = str(item.get("text") or "")
                    if itype == "agent_message" and text:
                        if step_texts and on_step:
                            on_step(len(step_texts))
                        step_texts.append(text)
                        reply.content += ("\n\n" if reply.content else "") + text
                        if on_content:
                            on_content(text)
                    elif itype == "reasoning" and text:
                        reply.reasoning += text + "\n"
                        if on_reasoning:
                            on_reasoning(text + "\n")
                    elif itype == "mcp_tool_call":
                        tool_seen = True
                    elif itype == "error" and item.get("message"):
                        notes.append(str(item["message"]))
                elif kind == "turn.completed":
                    done = ev
                elif kind == "turn.failed":
                    failure = str((ev.get("error") or {}).get("message") or "the turn failed")
                elif kind == "error" and ev.get("message"):
                    notes.append(str(ev["message"]))
            proc.wait(timeout=30)
            reply.elapsed_s = round(time.perf_counter() - started, 2)
            u = (done or {}).get("usage") or {}
            reply.input_tokens = int(u.get("input_tokens") or 0)
            reply.output_tokens = int(u.get("output_tokens") or 0)
            if failure or not done:
                text = failure or (notes[-1] if notes else "") or \
                    (b"".join(err).decode("utf-8", "replace").strip() or f"exit code {proc.returncode}")[-300:]
                hint = ("ChatGPT usage limit reached" if re.search(r"limit|quota|rate", text, re.I) else
                        "run `codex login` in a terminal and sign in with ChatGPT" if re.search(r"log ?in|auth|401", text, re.I)
                        else "")
                self._record(reply, False, source)
                recorded = True
                raise ApiError(None, text[:300], hint)
            reply.finish_reason = "stop"
            self._record(reply, True, source)
            recorded = True
            return reply, max(len(step_texts), 1), step_texts or [reply.content], (turn.called if turn else []), tool_seen
        except ApiError:
            if not recorded:
                self._record(reply, False, source)
            raise
        finally:
            if proc and proc.poll() is None:
                from .harnesses.codex import kill_tree
                kill_tree(proc)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            if turn_id:
                self.bridge.close(turn_id)
            for f in sorted(tmp.glob("*"), reverse=True):
                try:
                    f.unlink()
                except OSError:
                    pass
            try:
                tmp.rmdir()
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
        """A whole bot turn: Codex calls SnapLab's tools through the bridge until it has its answer."""
        system, blocks = split_messages(messages)
        reply, steps, step_texts, called, _ = self._run(system, blocks, tools=tools or None, execute=execute,
                                                        effort=effort, cancel=cancel, on_reasoning=on_reasoning,
                                                        on_content=on_content, on_step=on_step, on_wait=on_wait,
                                                        source=source)
        final = step_texts[-1] if step_texts and step_texts[-1].strip() else reply.content
        return LoopResult(text=final, stop_reason="done", steps=steps, input_tokens=reply.input_tokens,
                          output_tokens=reply.output_tokens, calls=1, first_input_tokens=reply.input_tokens,
                          step_texts=step_texts, tools_called=called)
