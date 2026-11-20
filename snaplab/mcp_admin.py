"""MCP servers added in Settings (phone or PC): kept in <data>/mcp_servers.json, merged with "mcp_servers" in
config.json. Secret environment values and tokens go to the OS credential store; the file only names them.

A server is tested before it is saved: SnapLab starts it (or connects to its URL) and lists its tools. Its tools then
reach chat bots (unless "chat" is off) and SnapCode sessions, Pi and Codex alike, through the snaplab_web bridge
(web_mcp.py), without a restart.

Accepted input:
- a command line ("npx -y @modelcontextprotocol/server-filesystem C:\\Users\\me\\Documents") with KEY=value lines,
- a URL (Streamable HTTP) with an optional token,
- the JSON most MCP servers document for Claude Desktop, Claude Code or Cursor: {"mcpServers": {"name": {...}}}.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from . import credentials
from .mcp_client import SERVER_RE, MCPError, make_client

FILE = "mcp_servers.json"
ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
TEST_TIMEOUT_S = 120          # `npx -y` downloads the package on first use


def read(app) -> dict:
    data = app.store.read_json(FILE, {})
    return data if isinstance(data, dict) else {}


def merged(app) -> dict:
    """Servers from config.json plus those added in Settings (Settings wins on a name clash)."""
    return {**(app.cfg.get("mcp_servers") or {}), **read(app)}


def secret(target: str) -> str | None:
    return credentials.read_credential(target)


def normalize_name(raw: str) -> str:
    name = re.sub(r"[^a-z0-9]", "", str(raw or "").lower())
    name = re.sub(r"^[0-9]+", "", name)[:16]
    if not SERVER_RE.match(name):
        raise ValueError("Name the server with 2 to 16 letters or digits, starting with a letter.")
    return name


def split_command(text: str) -> tuple[str, list[str]]:
    """Split a command line the way people paste it on Windows: quotes group words, backslashes stay."""
    parts = [a if a else b for a, b in re.findall(r'"([^"]*)"|(\S+)', str(text or "").strip())]
    if not parts:
        raise ValueError("Enter the command that starts the server.")
    return parts[0], parts[1:]


def parse_env(value) -> dict:
    if isinstance(value, dict):
        items = value.items()
    else:
        items = []
        for line in str(value or "").splitlines():
            if not line.strip():
                continue
            if "=" not in line:
                raise ValueError("Write environment values as KEY=value, one per line.")
            key, val = line.split("=", 1)
            items.append((key.strip(), val.strip()))
    env = {}
    for key, val in items:
        if not ENV_RE.match(str(key)):
            raise ValueError(f"{key!r} is not a valid environment variable name.")
        env[str(key)] = str(val)
    return env


def from_json(text: str) -> list[tuple[str, dict]]:
    """[(name, spec)] from the usual client configuration JSON, or a single server object."""
    try:
        data = json.loads(text)
    except ValueError:
        raise ValueError("The JSON could not be read. Paste the whole block, including the outer braces.") from None
    if not isinstance(data, dict):
        raise ValueError("Paste a JSON object.")
    group = data.get("mcpServers") or data.get("mcp_servers") or data.get("servers")
    if isinstance(group, dict):
        items = list(group.items())
    elif "command" in data or "url" in data:
        items = [(data.get("name") or "server", data)]
    else:
        items = [(k, v) for k, v in data.items() if isinstance(v, dict) and ("command" in v or "url" in v)]
    if not items:
        raise ValueError("No server found. Look for a block with \"command\" or \"url\".")
    out = []
    for name, spec in items:
        if not isinstance(spec, dict):
            continue
        kind = str(spec.get("type") or spec.get("transport") or "").lower()
        if kind == "sse":
            raise ValueError(f"{name}: the old SSE transport is not supported. Use the server's streamable HTTP URL "
                             "or its command.")
        token = None
        auth = (spec.get("headers") or {}).get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        out.append((name, {"command": spec.get("command"), "args": [str(a) for a in spec.get("args") or []],
                           "env": parse_env(spec.get("env") or {}), "url": spec.get("url"), "token": token}))
    return out


def _target(app, name: str, key: str) -> str:
    base = hashlib.sha256(str(Path(app.cfg["data_dir"]).resolve()).encode()).hexdigest()[:12]
    return f"SNAPLAB_MCP_{base}_{name}_{key}"[:200]


def tool_names(app, name: str) -> list[str]:
    return sorted(n for n, t in (getattr(app, "plugin_tools", None) or {}).items()
                  if (getattr(t, "mcp", None) or {}).get("server") == name)


def view(app) -> list[dict]:
    mine = read(app)
    errors = getattr(app, "mcp_errors", {}) or {}
    out = []
    for name, conf in merged(app).items():
        if not isinstance(conf, dict):
            continue
        out.append({"name": name, "label": conf.get("label") or name, "source": "settings" if name in mine else "config",
                    "kind": "command" if conf.get("command") else "url",
                    "command": " ".join([conf.get("command") or ""] + [str(a) for a in conf.get("args") or []]).strip(),
                    "url": conf.get("url"), "env_keys": sorted(set(conf.get("env") or {}) | set(conf.get("env_secrets") or {})),
                    "enabled": conf.get("enabled") is not False, "chat": conf.get("chat") is not False,
                    "tools": tool_names(app, name), "error": errors.get(name)})
    return out


def check(name: str, conf: dict, env: dict, token: str | None) -> list[dict]:
    """Start (or connect to) the server once and list its tools. Raises ValueError with a readable reason."""
    probe = {**conf, "env": env, "timeout_s": TEST_TIMEOUT_S}
    probe.pop("env_secrets", None)
    client = make_client(name, probe)
    if token is not None and hasattr(client, "token"):
        client.token = token
    try:
        return client.list_tools()
    except MCPError as e:
        raise ValueError(f"The server did not answer: {e}") from None
    finally:
        client.close()


def add(app, name: str, *, command: str | None = None, args=None, url: str | None = None, env=None,
        token: str | None = None, label: str | None = None, chat: bool = True, replace: bool = False) -> dict:
    name = normalize_name(name)
    if name in merged(app) and not replace:
        raise ValueError(f"A server named {name} already exists. Remove it first or choose another name.")
    env = parse_env(env or {})
    conf: dict = {"label": (str(label).strip() or name)[:40] if label else name, "chat": bool(chat)}
    if command:
        if args is None:
            command, args = split_command(command)
        conf.update(command=str(command), args=[str(a) for a in args])
    elif url:
        if not re.match(r"^https?://", str(url)):
            raise ValueError("The URL must start with http:// or https://.")
        conf["url"] = str(url).strip()
    else:
        raise ValueError("Enter a command or a URL.")
    tools = check(name, conf, env, token)
    for value in list(env.values()) + ([token] if token else []):
        if value and len(value) >= 8:
            app.guard.add(value)          # never repeated in replies, logs or tool results
    if env:
        conf["env_secrets"] = {}
        for key, value in env.items():
            target = _target(app, name, key)
            credentials.write_credential(target, value)
            conf["env_secrets"][key] = target
    if token:
        conf["bearer_secret"] = _target(app, name, "TOKEN")
        credentials.write_credential(conf["bearer_secret"], token)
    data = read(app)
    data[name] = conf
    app.store.write_json(FILE, data)
    app.reload_mcp()
    app.log(f"MCP server {name} added in Settings ({len(tools)} tools)")
    return {"server": name, "tools": [t.get("name") for t in tools], "servers": view(app)}


def add_json(app, text: str, chat: bool = True) -> dict:
    results = []
    for raw_name, spec in from_json(text):
        try:
            name = normalize_name(raw_name)
            if spec.get("command"):
                r = add(app, name, command=spec["command"], args=spec["args"], env=spec["env"], label=raw_name, chat=chat)
            else:
                r = add(app, name, url=spec.get("url"), token=spec.get("token"), label=raw_name, chat=chat)
            results.append({"name": name, "ok": True, "tools": r["tools"]})
        except ValueError as e:
            results.append({"name": str(raw_name), "ok": False, "error": str(e)})
    return {"results": results, "servers": view(app)}


def remove(app, name: str) -> dict:
    data = read(app)
    conf = data.pop(name, None)
    if conf is None:
        raise ValueError("Only servers added in Settings can be removed here; others are in config.json.")
    for target in list((conf.get("env_secrets") or {}).values()) + [conf.get("bearer_secret")]:
        if target:
            credentials.delete_credential(target)
    app.store.write_json(FILE, data)
    app.reload_mcp()
    app.log(f"MCP server {name} removed in Settings")
    return {"servers": view(app)}


def update(app, name: str, *, enabled=None, chat=None) -> dict:
    data = read(app)
    if name not in data:
        raise ValueError("Only servers added in Settings can be changed here; others are in config.json.")
    if enabled is not None:
        data[name]["enabled"] = bool(enabled)
    if chat is not None:
        data[name]["chat"] = bool(chat)
    app.store.write_json(FILE, data)
    app.reload_mcp()
    return {"servers": view(app)}


def retest(app, name: str) -> dict:
    """List the tools again (a server that was down at start, or one that changed)."""
    app.reload_mcp(start_timeout_s=TEST_TIMEOUT_S, only=name)
    return {"servers": view(app)}
