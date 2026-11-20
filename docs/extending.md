# Extending SnapLab

SnapLab has five extension points. None of them needs a change to SnapLab's own code, and each one is
checked when SnapLab starts (a broken extension is skipped and written to `data/logs/server.log`; it never
stops the server).

| What | Where | Checked by |
|---|---|---|
| Providers and models | `config.json`: `providers`, `models`, `routing` | `snaplab/models.py` `check()` (SnapLab refuses to start on a bad setting) |
| Session harnesses | a Python class, listed in `config.json` `harness_plugins` | `snaplab/harnesses/contract.py` `problems()` |
| Tools for bots | one Python file in `plugins/tools/` or `<data>/plugins/tools/` | `snaplab/plugins.py` `validate()` |
| UI languages | one file `web/i18n/<code>.js` | `tests/test_extend.py` (same keys as English) |
| Context builder | `config.json` `context_builder: "module:Class"` | the default builder in `snaplab/context.py` is the reference |

After any change: `python snaplab.py stop`, `python snaplab.py start`, and run the tests
(`python -m unittest discover -s tests -v`; no model is called).

---

## 1. Providers and models

SnapLab talks to OpenAI-compatible **Chat Completions** endpoints (`POST <base_url>/chat/completions`,
streaming). You can configure any number of them and choose a model per bot and per kind of call.

```jsonc
// config.json
{
  "providers": {
    "snapscribe": {                                   // example; setup's Snapscribe preset writes a similar entry
      "base_url": "https://api.snapscribe.example/v1",  // the real address lives in snaplab/presets.py
      "api_key_env": "SNAPSCRIBE_API_KEY",            // the NAME of the environment variable, never the key
      "slots": "shared"                               // use the shared slot folder and count ("slots" in config)
    },
    "other": {
      "base_url": "https://api.example.com/v1",
      "api_key_env": "OTHER_API_KEY",
      "user_agent": "snaplab/0.1",                    // optional
      "timeout_s": 600,                               // optional
      "slots": {"count": 4},                          // its own concurrency limit (lock files in data/locks/other)
      "key_expires": "2027-01-31"                     // optional, shown on the dashboard
    }
  },
  "models": {
    "main":  {"provider": "snapscribe", "model": "your-model-id", "context_limit": 131072,
              "effort_style": "chat_template_kwargs", "label": "Snapscribe 1"},
    "fast":  {"provider": "other", "model": "some-model-id", "context_limit": 32768,
              "effort_style": "reasoning_effort", "label": "Fast model",
              "extra": {"temperature": 0.3}}          // optional fields added to every request
  },
  "routing": {"default": "main", "chat": "main", "session": "main", "codex": "main"}
}
```

- **Keys** use Windows Credential Manager, macOS Keychain or Linux Secret Service after first-run setup.
  Process environment variables can override these; legacy Windows registry environment values remain a fallback. SnapLab reads the variable named by `api_key_env` at call time. Every key value is
  added to the exit guard, so it is removed from anything SnapLab stores, commits, shows or notifies.
- A fresh install shows Snapscribe selected, with its URL and the Snapscribe 1 model id filled in. It sends nothing
  until the user enters a key and tests the connection. The setup screen writes only provider/model metadata to
  `<data_dir>/provider.json`; it never writes the key. The default API concurrency is two requests, with locks
  inside that installation's data folder. Explicit `config.json` values and shared lock folders are preserved.

- **effort_style** says how the thinking depth is sent:
  `chat_template_kwargs` (`chat_template_kwargs.reasoning_effort`, for vLLM-style servers; Snapscribe: `low`,
  `medium`, `xhigh`), `reasoning_effort` (OpenAI style, top-level field; `xhigh` is sent as `high`) or `none`
  (nothing is sent). Omitted model effort styles mean `none`. The Snapscribe preset in setup uses
  `chat_template_kwargs`; other providers do not receive those fields.
- **routing** picks the model by kind of call: `chat` (bots talking in rooms), `session` (the "Bot only"
  harness) and `codex` (the Codex and Pi harnesses, through SnapLab's translator). Missing kinds use `default`.
- **Per bot:** a bot's page shows a model menu when more than one model is configured. The bot's choice is
  used for its chat turns and its sessions. `routing` is used when the bot has no choice.
- **Codex and Pi** get the model through the local translator (Responses API to Chat Completions). The session's
  config names the registry entry (`model = "main"`) and its `context_limit`; the translator looks the
  entry up and calls that provider with that provider's key. The agent itself only ever holds a random
  per-run token.
- **Usage** is counted per model (dashboard and `data/usage.json`, `by_model`).
- Settings shows every model, its provider, context size, whether its key is set, and what it is the
  default for.

The old single `"api"` block still works: SnapLab turns it into one provider and one model.

### Add providers in Settings

Settings → Models → Add a provider and model opens the same setup form for existing installations,
including Tailscale installations. The four choices are Snapscribe, OpenRouter, OpenCode Zen / Go,
and another OpenAI-compatible Chat Completions API. Each registration has its own credential target;
changing the URL or provider clears the unsaved key. Catalog requests and inference never follow redirects.
The existing default and bot selections stay unchanged. Choose the new model on the bot's profile.
New sessions pin that choice, so later profile edits do not reroute running work.

OpenRouter uses `https://openrouter.ai/api/v1`. OpenCode Zen uses `https://opencode.ai/zen/v1`;
Go uses `https://opencode.ai/zen/go/v1` and requires a separate subscription. The form appends
`/models` for discovery and `/chat/completions` for the probe. It rejects known Responses/Messages
models and unknown OpenCode families. OpenCode's current Console documentation also describes newer
`/inference` URLs and protocol translation; those combinations are not verified by this integration.
Use the custom API option only after checking its Chat Completions contract.

The compatibility section lets users set the context limit, tool and image support, streaming usage,
and output parameter (`max_tokens`, `max_completion_tokens`, or omitted). No automatic retry changes
parameters. These are declared capabilities, not guarantees established by the tiny connection probe.
Bot and SnapCode require tool support; a model without it produces a clear error when tools are requested.
Model catalog metadata fills the available capability fields. Unknown image support defaults to off.

Models may specify `capabilities: {"tools": true, "vision": true, "stream_usage": false,
"token_parameter": "max_completion_tokens", "reasoning_history": false, "max_output_tokens": 8192,
"protocol": "chat"}`. Bot and translator apply this table before sending. Estimated input, reserved output
and a safety margin must fit the model context; oversize input is rejected, not silently discarded.
Image payloads are budgeted as images rather than base64 text. Usage may be absent and is then recorded as
zero reported tokens, not as a claim that the provider charged nothing.

External registrations use two independent slots. Only Snapscribe registrations share the installation's
configured Snapscribe pool. An explicit `slots` setting in `config.json` (count or shared folder) is kept; fresh
installations use two slots. A missing/removed model fails instead of falling back.
DM summaries and memory tidy use the bot's selected model. Mixed-model groups skip automatic summaries
instead of choosing a different destination implicitly.

After the user registers a real external key, run:

```powershell
python tools/check_provider.py --config C:\path\to\config.json --model openrouter-REGISTRATION_ID
# Optional: also run one real SnapCode job on an SSH worker, at most four additional model requests:
python tools/check_provider.py --config C:\path\to\config.json --model openrouter-REGISTRATION_ID --ssh-host WORKER_HOST --ssh-exe ssh
```

The first command makes four small calls: conversation, tool request, tool result, and SnapCode translation.
Only synthetic messages are used, with no retries. It reports booleans and counts, never response text or
keys. The optional SSH test runs in fresh disposable data and keeps provider keys on the SnapLab PC.
For vision, select an image-capable model in the UI and ask a test Bot to read an artificial image;
then ask a test SnapCode to take and view its private desktop screenshot. Do not use personal data.

References: [OpenRouter API](https://openrouter.ai/docs/api_reference/overview),
[OpenCode model endpoints](https://opencode.ai/docs/zen/), [OpenCode Go](https://opencode.ai/docs/go/).

**Check:** `python -c "from snaplab import config, models; c = config.load(); print(models.check(c) or 'ok')"`.

---

## 2. Session harnesses

The WSL workspace and the SSH backend also run `pi`, pinned to `@earendil-works/pi-coding-agent@1.5.0`.
`tools/setup_worker.sh` installs it under the worker account's `.local`; on an SSH worker the worker account can
run `sh tools/setup_worker.sh <worker-user> --pi-only` itself. Pi needs Node.js 22.19 or newer; setup installs
Node.js 22.22.1 in `.local` when necessary. After updating SnapLab, existing WSL installations rerun
`tools/setup_wsl.py --resume` (or `install.ps1 -SetupWorker`) to refresh their checked setup version.

`start_session` normally omits `harness`. With `codex.backend: "ssh"` or `"wsl"`, ordinary
code, document and web application tasks select Pi; desktop operation selects
Codex. An explicit user request takes precedence. The Windows `local` backend keeps Codex.
`task_type` can identify `code`, `web`, `desktop` or `research`; web tasks receive
instructions to plan, implement and verify in stages. `required_files` records
the outputs that must exist before completion. A configured project test command
must pass, and the final explanation must be present. Exit status zero alone is
insufficient. Incomplete output and uncertain network failures never become success.

Both adapters use the same Bubblewrap boundary, job-scoped proxy token,
shared API slots, per-turn model-call limit (`sessions.max_api_calls`, none by default),
validated transfer and cancellation path.
Pi disables project configuration, extensions and provider retries; its trusted
bash tool strips authentication variables before starting subprocesses. A failed
startup with no file progress may continue in Codex once, using the remaining
model calls; it counts as one more session start toward the bot's hourly limit,
when one is set. Uncertain requests are never resent. Partial remote files survive
cancellation; a conflicting next upload preserves them in a sibling recovery
directory before replacing the working copy.

Follow-up requests use `send_to_session`, the existing files and `.snaplab/PLAN.md`,
plus up to 16,000 characters of prior prompts and final explanations. Provider
transcripts and authentication files are not saved in the project. Both adapters
receive the same web MCP tools; Pi's tool names are explicitly enabled at launch.
Normal result messages omit harness names; the session details retain them.
`send_to_session.test_command` can correct the project's test runner command;
editing BRIEF.md alone does not change that setting. Use `python3` on Linux.

`api.work_effort` defaults to `medium`, `api.desktop_effort` to `low`, and
`api.work_max_tokens` to 20,000. The output limit is a ceiling per response,
not a minimum allocation; shared concurrency limits still apply.

A harness runs one turn of a session in the session's own folder (a git worktree when the project has git).
SnapLab ships four:

| id | What it does | Runs code |
|---|---|---|
| `codex` | the Codex CLI with the configured model: in the WSL workspace or on an SSH worker, or with the `local` backend inside the Windows mxc sandbox (session folder only, no network) | yes, sandboxed |
| `pi` | the Pi coding agent with the configured model, in the WSL workspace or on an SSH worker (loaded only with those backends) | yes, sandboxed |
| `bot` | a SnapLab bot that researches and writes notes and files (no code execution) | no |
| `tests` | runs the project's test command in the Windows mxc sandbox; no model. The smallest complete example | yes, sandboxed |

### The contract

The contract is written in code in `snaplab/harnesses/contract.py` and checked when a harness is loaded.

**A harness is a class with:**

| Member | Meaning |
|---|---|
| `id: str` | 2-31 lowercase letters, digits, `-` or `_`; unique |
| `label`, `description: dict` | `{"en": ..., "ja": ...}`; English is required, other languages fall back to it |
| `icon: str` | optional icon name from `web/app.js` `ICONS` (`bot`, `terminal`, `flask`, ...) |
| `runs_code: bool` | `True` if it executes code or commands |
| `sandboxed: bool` | must be `True` when `runs_code` is `True`, and every command must really run in a sandbox. A harness that runs code without declaring this is **not loaded** |
| `__init__(self, app)` | gets the SnapLab app (config, store, models, guard, ...) |
| `available(self) -> (bool, str)` | whether it can run on this PC, and why not (shown on the phone) |
| `run_turn(self, session, folder, prompt, ctl, first) -> TurnResult` | one turn, called in a background thread |

**What `run_turn` receives:**

- `session`: the session record (read it; save your own fields with `ctl.update(...)`, for example a thread id
  to resume the next turn).
- `folder`: the session's folder. Work only here.
- `prompt`: the instruction for this turn. For `codex` the first turn gets a brief built by SnapLab
  (project BRIEF, rules, the user's instruction).
- `ctl`: the session control object:
  - `ctl.emit({"kind": ..., "text": ...})`: an event for the timeline. Kinds: `message`, `command` (may add
    `exit_code`, `output`), `file`, `tool`, `tests` (may add `passed`, `failed`), `note`, `error`, `thinking`.
  - `ctl.update(**fields)`: store fields on the session.
  - `ctl.cancelled()`: `True` once the user pressed Stop. Check it often and return quickly.
  - `ctl.set_process(popen)`: register a child process so Stop can kill its whole tree.
- `first`: `True` on the first turn of the session.

**What it returns:** `TurnResult(ok, last_message="", error=None, asks_user=False, input_tokens=0,
output_tokens=0, calls=0, extra={})`. `extra` may carry `{"tests": {...}}` in the shape of
`snaplab.testparse.parse_tests()` when the harness ran the tests itself; SnapLab then does not run them again.

**How status is reported:** the harness does not set the session status. SnapLab derives it from the result:
`ok` gives **review** (or **waiting** when `asks_user` is true), not `ok` gives **failed**, Stop gives
**stopped**. Around every turn SnapLab itself snapshots the folder, removes secret values (exit guard),
commits to the session branch, measures the diff, runs the project's tests for coding harnesses, writes
`result.json`, posts a short result card to the room that started the session and sends notifications.
An exception inside `run_turn` becomes a failed turn with the error text.

### Add a harness, step by step

1. Write the class, for example `my_harnesses/echo.py`:

   ```python
   from snaplab.harnesses.base import Harness, TurnResult

   class EchoHarness(Harness):
       id = "echo"
       label = {"en": "Echo", "ja": "おうむ返し"}
       description = {"en": "Writes the instruction into ECHO.md. No model, no code."}
       icon = "page"
       runs_code = False

       def available(self):
           return True, ""

       def run_turn(self, session, folder, prompt, ctl, first):
           (folder / "ECHO.md").write_text(prompt, encoding="utf-8")
           ctl.emit({"kind": "file", "text": "ECHO.md"})
           return TurnResult(ok=True, last_message="Wrote ECHO.md.")
   ```

2. Make the module importable (put the folder next to `snaplab.py`, or on `PYTHONPATH`) and list it in
   `config.json`: `"harness_plugins": ["my_harnesses.echo:EchoHarness"]`.
3. Check it: `python -c "from my_harnesses.echo import EchoHarness; from snaplab.harnesses import contract; print(contract.problems(EchoHarness(None)) or 'ok')"`
   (pass a real app if `available()` needs one), and add a test like `HarnessContractTest` in
   `tests/test_extend.py`.
4. Restart SnapLab. The harness appears on the New session sheet, and bots can pick it in `start_session`
   (the tool's harness list follows the loaded harnesses).

**If your harness runs code:** set `runs_code = True` and `sandboxed = True`, and run every command
through `snaplab.harnesses.codex.sandbox_run(app, session_id, folder, command, timeout_s, git_dir)`. It uses
the same per-session mxc permission profile as Codex on the Windows backend: the session folder is the only readable and writable
place (plus the Python install and the project's `.git`, read only), there is no network, and SnapLab's data
and the user's home are unreadable. `snaplab/harnesses/tests.py` is a complete example in 50 lines.
`tests/test_sandbox.py` shows how to prove the read limits on a real PC.

---

## 3. Tools for bots

`message_bot`, `create_group`, `share_project` and `read_brief` are built-in names.
All bots can use `create_project` and `write_brief`; only the lead can use `assign_project` and `create_bot`.
Use `snaplab.access.project_ids(app, bot, manage=False)` or `check_project(..., manage=True)` when extending
project tools; never truncate a bot's projects to its first entry. Shared projects grant reading only.
Bot-message extensions must go through `ChatManager.message_bot` and retain the current origin, so they
cannot bypass public history, stop handling, conversation budgets or the shared model slots.

`bot_conversations` configures `max_consecutive` (6) and `repeat_window` (20), each a positive integer, and
`max_hourly` (0, meaning no limit). Counts include silent/error group attempts and outgoing messages. The consecutive budget follows
the original human message across rooms. Hourly limits apply per bot across groups. `rooms.default_responder`
is retained for old configuration files but no longer restricts participation: all members can choose `[SILENT]`.
Successful external reads set `ctx.read_web` for this turn and subsequent bot turns in the same origin;
do not infer it from history text. Preserve origin when delivering to other bots. Untrusted plugin results
are archived with `web_material: true`, so later recall also requires approval for protected actions.
The plugin tool name is recorded in the proposal. Internal tool output does not count as external material.
Session toolboxes default to `session:<id>` as their origin; automatic result reports inherit its reads,
including those made through SnapCode's web MCP tools. Keep the session id, title and turn in the read
metadata, so approval reasons identify the work that read external material.

A tool is one Python file. Drop it into `plugins/tools/` (in the repository) or `<data_dir>/plugins/tools/`
(this PC only), or set `"tool_dirs": [...]` in `config.json`. Files starting with `_` are ignored.

```python
# plugins/tools/shout.py
TOOL = {
    "name": "shout",                                   # 3-41 lowercase letters, digits or _; no clash with built-ins
    "description": "Upper-case a text.",               # what the model reads
    "parameters": {"type": "object",                   # JSON schema of the arguments
                   "properties": {"text": {"type": "string"}}, "required": ["text"]},
    "scope": ["chat"],                                 # chat (rooms) and/or session ("Bot only" sessions)
    "approval": "never",                               # never | after_web | always: a proposal card first
    "orchestrator_only": False,
    "label": {"en": "Shouted", "ja": "叫んだ"},          # the verb on the phone's tool chip
    "untrusted": False,                                # True if the result comes from outside (wrapped as data)
}

def run(ctx, text):
    return {"text": text.upper()}                      # a dict is sent to the model as JSON; a str as is
```

`ctx` gives `ctx.app`, `ctx.bot`, `ctx.room` (chat) or `ctx.session` (sessions), `ctx.folder` (the session
folder, sessions only), `ctx.lang`, `ctx.read_web` (external reads in this turn or inherited from this origin) and
`ctx.guard` (the exit guard, to clean any text you store elsewhere).

- **Approval:** `always` shows a proposal card before every use; `after_web` applies after an external read
  in this turn or the same human-origin conversation. `config.json` `approvals: {"shout": "always"}` overrides the plugin's own choice.
  An approved card runs the tool once; declining runs nothing.
- **Errors:** raise `ValueError` (or any exception) with a short message; the bot sees
  `Error: <message>` and can try something else; the turn goes on.
- **Trust:** plugins run inside the SnapLab process with its permissions, like any Python you install.
  Add only code you trust, and keep tools free of shell or code execution: running code is what the
  sandboxed harnesses are for.
- **Keep them quick:** a tool runs inside SnapLab, so a slow one holds up everything else (other rooms, the phone's
  updates). Refuse work that can explode in size before doing it, as the calculator does for huge numbers, and
  call outside services with a timeout.
- **Example:** `plugins/tools/calculator.py` (exact arithmetic with a small expression evaluator, no `eval`).
- **Check:** start SnapLab and look for `tool plugin ... not loaded` in `data/logs/server.log`, or write a
  test like `PluginToolTest` in `tests/test_extend.py`.

### External MCP servers

The easy way is **Settings > Tools (MCP servers) > Add a server**: paste the JSON block a server's README gives
for Claude Desktop, Claude Code or Cursor (`{"mcpServers": {...}}`), or enter a command line or a URL. SnapLab
starts the server once to list its tools before it saves anything, keeps secret environment values and tokens in
the OS credential store (the file `data/mcp_servers.json` only names them), and applies the change without a
restart. Stdio servers (`command` and `args`) run on this PC as the user and stay running; SnapCode reaches them
through SnapLab, so the isolated workspace never runs them itself.

In `config.json` the same servers can be written by hand, and Streamable HTTP servers work too:

```json
"mcp_servers": {
  "notes": {"url": "http://127.0.0.1:8765/mcp",
            "approvals": {"delete_note": "always"}},
  "files": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:\\Users\\me\\Documents"]}
}
```

- Each tool is named `<server>_<tool>` (`notes_search`). Server names are 2-16 lowercase letters
  or digits, starting with a letter.
- **Who gets the tools:** chat bots and SnapCode. Chat bots call them directly, see a "Connected services"
  list, and a reply claiming a result they did not fetch is sent back once. SnapCode gets them in "bot"
  sessions directly and in Codex and Pi through the `snaplab_web` MCP server (SnapLab relays each call, so the
  isolated workspace needs no route to the service); bots are told they can hand big jobs to a session to keep
  the chat light. `"chat": false` keeps a server for SnapCode only (chat bots are then only told it exists).
- Sessions run unattended, so tools that would show a proposal card are not offered to them at all.
- Tools run directly; only tools the server marks `destructiveHint` show a proposal card first. `approvals`
  per tool (or the global `approvals` block, by the SnapLab tool name) overrides that; `exclude` hides tools.
- Results are untrusted material by default (`"untrusted": false` to change), so later protected actions in
  the same conversation ask first, as after a web read.
- `label` is the name people and bots see (`"Notes (shared)"`). `about` replaces the server's own
  instructions in the bots' "Connected services" list: use it to say what this installation uses the server
  for, especially when two entries point at the same kind of server (for example two accounts on one service).
- `bearer_env` names an environment variable whose value is sent as `Authorization: Bearer ...`.
- The tool list is cached in `data/mcp_tools.json`: if the server is down when SnapLab starts, bots keep the
  tools and a call returns an error they can explain. **Check** in Settings lists the tools again.

---

## 4. Languages

Every string SnapLab shows or writes lives in `web/i18n/<code>.js`, one `"key": "value",` per line, in the
same order in every file. `en.js` is the reference. That includes what the server writes in the user's
language: the welcome message, the default persona prompt and tone descriptions (`srv.*`), activity lines
(`act.*`), the notes after an approval (`note.*`), proposal cards (`prop.*`), notification titles (`push.*`),
timeline lines (`ev.*`) and error explanations (`err.code.*`).

1. Copy `web/i18n/en.js` to `web/i18n/<code>.js` (`fr.js`, or `pt_br.js` for a regional variant).
2. Change the line `window.SNAPLAB_I18N.en = {` to `window.SNAPLAB_I18N.<code> = {`.
3. Translate the values. Keep the keys and the `{placeholders}` exactly. Set the language keys at the top:
   `lang.name` (the language's own name, shown in pickers), `lang.english` (its English name, used to tell the
   bots which language to answer in), `lang.hello`, `lang.locale` (for dates, e.g. `fr-FR`). If the language
   adds a polite form to names, set `srv.call.honorific` (for example `{call}さん`) and the endings that already
   are polite in `srv.call.keep`; otherwise leave them as in English.
4. Run `python -m unittest discover -s tests -p "test_extend.py" -v` and
   `python -m unittest discover -s tests -p "test_round4.py" -v`: they fail if your file misses a key, has extra
   ones, or lacks a key the server uses.
5. Check plural grammar, stop phrases, script detection, bundled avatar names and harness labels before shipping.
   Add plural variants as flat keys (`rc.files.one`, `.few`, `.many`, `.other`) when the language needs them.
   The browser uses `Intl.PluralRules`; add the matching cardinal rule to `snaplab/i18n.py` for server text.
   Counts belong in `vars.n`, including stored events. Older single/multiple keys are mapped at display time;
   never rewrite a user's saved events merely to translate them. Existing English/Japanese keys retain their output.
6. Restart SnapLab. The language appears on the first screen and in Settings. Test setup, pairing, offline
   pages, notifications, mobile layout, bot tools and result reports in that language too.

Russian (`ru`) uses the polite form of address and CLDR cardinal categories. Language selection does not change
the time zone. Run `python tools/test_offline.py` and `node tests/round17_ui.mjs` for offline regression and UI
checks.

How it fits together: the server joins `web/i18n/core.js` and all language files into the single `/i18n.js`
the phone loads, reads the same files for its own texts (`snaplab/i18n.py`), and accepts any language code
that has a file. A missing string falls back to English. Activity lines, approval notes, proposal cards and
timeline lines are stored with their key and values (a value written `@some.key` is itself a key), so the
phone shows them in the language it is set to now, even if they were written in another one. What the
model reads (house rules, tool descriptions, result cards) stays in English on purpose; the bots are told to
answer in the selected language (`Language: <English name>` in their persona).

---

## 5. Context builder

The default builder (`snaplab/context.py`) assembles each model call from files, keeping what rarely changes at
the front of the system prompt (persona, house rules, tool tips, who is in the room, notes about the user, memory,
BRIEFs, room summary) and putting what changes every minute (time, projects, recent sessions with ids, dashboard
widget ids) in a short block at the top of the latest message, so the server can reuse the long prefix. Budgets
are in `config.json` `context` (measured defaults: `target_tokens` 16000, `summarize_tokens` 24000, `max_tokens`
32000, `session_max_tokens` 64000). To replace it, write a class with the same
`build(room, bot, lang) -> (messages, report)` method (and optionally `learn(room_id, estimate, real)`) and set
`"context_builder": "module:Class"`. The report feeds the "What this bot saw" screen. If you write your own, keep
the changing parts out of the front of the prompt: a timestamp there makes every call start from scratch.

## 6. Searching the record (recall)

The raw archive is plain JSONL (`data/archive/`), so another search can be added without touching it. Write a
class with the methods of `snaplab.archive.FtsIndex` (`add(record)`, `has(key, hash)`, `search(query, scope,
kinds, limit)`, `get(id)`, `count(scope)`, `close()`), and set:

```json
"recall": {"index": "mypackage.embed_index:EmbedIndex"}
```

`scope.sql()` / `scope.allows(record)` tell which records a bot may see; keep that check. Delete
`data/archive/index.sqlite3` to have the built-in index rebuilt from the files.

Search also receives a `TemporalScope`. Its SQL refers to `recorded_at`, `valid_from`, `valid_to`,
`end_recorded_at` and `lifecycle`; a replacement index must implement equivalent effective-time,
knowledge-time and supersession rules before applying its candidate limit. Returned metadata must include
the effective interval, not only the original record's unclosed interval. The archive checks `allows()`
again before returning candidates. The optional `latest(key)` returns the latest snapshot for provenance
and version links. `has(key, hash)` must compare the latest version, so reverting to earlier text creates
a new version. Keep original IDs and source permissions when adding embeddings later.

Harness test evidence must include `tested_version`, `version_after`, `exit_code`, `source_type="runtime"`
and an `evidence_record`. Use `snaplab.evidence.artifact_version()` before and after execution and
`record_tests()` for the guarded original output. A natural-language claim or a test result lacking its
revision remains unverified. The standard test harness demonstrates the full path.

## 7. Icons

Every interface icon is one SVG file in `web/icons/ui/` (24 x 24, `currentColor`). Replace the files (keep the
names) and reload: the server builds the sprite from the folder. `tests/test_round6.py` checks that every icon the
app uses has a file. A harness can name its icon with `icon = "<file name>"`.

## 8. SnapCode steps from a harness

A harness shows progress by writing `.snaplab/PLAN.md` in its work folder: one checklist line per step,
`- [ ]` for open and `- [x]` for done. SnapLab reads it while the turn runs and after it ends; nothing else is
needed.

## 9. SnapCode web tools

`snaplab/web_mcp.py` shares the existing web tools (and the external MCP tools that may run unattended) with
Codex and Pi on the translator's loopback port.
The generated `[mcp_servers.snaplab_web]` table uses `url`, `bearer_token_env_var`, `enabled_tools`,
`required`, and timeouts as documented in [OpenAI's Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).
This uses only Python's standard library; there is no new MCP SDK dependency.

Do not expand the allowlist merely by adding a tool to `Toolbox`: the bridge deliberately exposes only
search, page reading and the external MCP tools offered to sessions. Any new outbound argument must be checked before use, must retain the existing
private-address/size protections, and must have tests proving that secrets never reach a fake transport.
Do not enable command networking in the Windows sandbox as a way to fix an MCP startup error. A missing
required bridge should fail the Codex turn, not weaken its permission profile. Verify the installed CLI's HTTP MCP handshake outside
the sandbox after changes. Fresh and resumed turns must both receive a new capability.

Codex maintains `.snaplab/KNOWLEDGE.md` in its worktree; SnapLab archives its changed contents after a turn.
It is not automatically merged into project main because `.snaplab` is excluded from Git. The archive keeps
the research with its session/project scope. Use an ordinary project document if findings should ship in the
project, while retaining sources and treating copied web claims as untrusted.
