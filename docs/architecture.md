# SnapLab architecture

SnapLab is one Python process on your PC plus a phone web app. No framework, no build step.

```
Phone (installed PWA) ──HTTPS, tailnet only──▶ tailscale serve :8445 ──▶ 127.0.0.1:18445  SnapLab server
                                                                          │
   ┌──────────────────────────────────────────────────────────────────────┤
   │ server.py      routes, two-step gate (Tailscale owner + device cookie), SSE stream
   │ app.py         wiring; onboarding, bots, projects, dashboard, approvals
   │ chat.py        rooms, who answers, streaming, question cards, group-room rules
   │ context.py     builds what a bot sees on each call (pluggable)
   │ agent.py       the tool loop (model → tools → model … → answer)
   │ tools.py       the tools bots can call (and their safety rules)
   │ sessions.py    sessions in project folders, turns, changed files, tests, states
   │ harnesses/     codex.py (Codex CLI), pi.py (Pi), remote.py + remote_worker.py (isolated Linux jobs),
   │                wsl.py (the local WSL workspace), desktop.py + browser.mjs (computer use),
   │                bot.py (talk and research), tests.py (run tests),
   │                base.py (interface), contract.py (the contract, checked at load)
   │ proxy.py       translator for Codex and Pi: Responses API ⇄ Chat Completions (127.0.0.1:18432)
   │ web_mcp.py     SnapCode's MCP endpoint for web search, page reading and external MCP tools
   │ mcp_client.py  external MCP servers as bot tools
   │ models.py      providers and models: which endpoint, model, key variable and slots for each call
   │ llm.py         Chat Completions client (streaming, thinking depth, cut-stream detection, no retries)
   │ plugins.py     tool plugins from plugins/tools/*.py
   │ i18n.py        UI language files (web/i18n/<code>.js) joined into /i18n.js
   │ slots.py       concurrency limit shared through lock files
   │ privacy.py     hides private values before anything leaves the PC
   │ websearch.py   keyless search (DuckDuckGo), Brave/Tavily by env var, page reader
   │ memory.py      long-term memory files: each bot's notes, the shared notes about the user, the daily log
   │ archive.py     the append-only record and its search index
   │ tasks.py       scheduled tasks and wake-ups
   │ dashboard.py   widget validation (data only)
   │ push.py        Web Push (VAPID + aes128gcm) with the cryptography package
   │ store.py       JSON/JSONL files in the data folder; the event log
   └ bus.py         in-process events → SSE
```

An installation made by the installer starts in local mode: the server answers only the PC's own browser on
`127.0.0.1`, without Tailscale. Phone access goes through `tailscale serve` as drawn above.

## Data

Everything lives in `data/` (git-ignored): settings, bots and their `memory.md` (plus `daily/<date>.md`, an
append-only log of memory changes), `shared/user.md` (what every bot knows about the user), rooms and message logs,
question cards, projects, sessions and their timelines, dashboard widgets, activity, usage per day, push
subscriptions and the VAPID key, pairing hashes, and `events/<day>.jsonl`, an append-only log of who did what
and where (messages, tool calls, cards, session turns, memory writes, dashboard changes). Projects are folders
under `projects_dir` (default `data/projects/`); each gets an `AGENTS.md` describing the project and its rules.

## A chat turn

1. The phone posts a message. The server stores it and queues the room's bot (one-to-one), or all group
   members with mentioned bots first. Each group member decides whether it has something useful to add.
2. `context.py` builds the call. The system prompt holds what rarely changes, most stable first: the bot's
   persona, the house rules (plus the group-room rules), tool tips, who is in the room, the notes about the user,
   the bot's memory, the BRIEFs of its projects and the room summary. Then the recent history (what the summary
   does not cover, at least a few messages). What changes every minute (the time, projects, sessions of the last
   24 hours with their ids, status and tests, dashboard widget ids) is a short "SnapLab context" block at the top
   of the latest message. The server reuses an unchanged prefix, so keeping the time out of the system prompt
   keeps repeated calls fast (measured: a 64K context came back in 3-5 s the second time). Other bots' messages
   appear as `[Reference] Name said: …`. A size report per section is stored with the message.
3. `agent.py` streams the model's thinking and text to the phone (batched every ~80 ms) and runs tool calls
   until the model answers. Thinking depth: the first call of a turn is `low`; once the turn has called a tool,
   the next calls are `medium` (measured: with low the bot kept working after the task was done in 3 of 5 tasks,
   with medium in none, and it finished sooner). If the final reply says it remembered something, changed the
   dashboard or started a session without calling that tool, the model gets one note to do it or correct itself.
   Older tool results are shortened once a turn passes `summarize_tokens`. A reply whose action waits for a card
   says so under the bubble ("nothing has been done yet").
4. The reply is saved when it starts (status `streaming`) and again when it ends; both writes share one id,
   and the later one wins in place. After a restart, a reply that was still `streaming` becomes an error with
   the code `restart`, so the room shows "SnapLab restarted while this reply was being written" and
   **Try again** instead of an unanswered question. A stream that ends without its end marker keeps the text
   that arrived and is marked `cut`. Every failed reply carries a short code (`busy`, `server`, `timeout`,
   `unreachable`, `key`, `nokey`, `too_long`, `cut`, `restart`, ...) that the phone turns into a sentence in
   the user's language, with the raw error folded under "Details". **Try again** runs the turn once more;
   the failed reply is then left out of the bot's context.
5. Messages sent while a bot is busy are not answered one by one: when a queued turn starts and the bot's
   latest finished reply already comes after every user message, the turn is skipped. So five quick
   messages cost two calls, not five, and the second call sees all of them.
6. Group contributions and explicit bot deliveries share the configurable cross-room limits
   (`bot_conversations`, see "Project ownership and bot conversations" below).
   A bot with nothing to add answers `[SILENT]` (hidden); `@user` flags the message as needing you.

## Sessions

`sessions.py` runs one turn at a time per session in a background thread: snapshot the folder, run the
harness, diff the folder, run the project's test command inside the sandbox, set the state, announce it.

| State | Meaning | Colour |
|---|---|---|
| Working | a turn is running | blue, animated |
| Needs you | the agent ended with a question | amber |
| Ready for review | finished; not reviewed yet | green |
| Failed | the turn failed | red |
| Stopped | you stopped it | grey |
| Idle | you marked it reviewed | faint |

Lists are ordered by urgency (Needs you → Failed → Ready for review → Working → Stopped → Idle). Any
finished session takes a follow-up instruction. On the Windows backend Codex continues with
`codex exec resume <thread>`; in the WSL workspace (and with the SSH backend) a follow-up gets the existing files,
`.snaplab/PLAN.md` and the earlier prompts and final explanations.

**Harness interface** (`harnesses/base.py`): a class with `id`, `label`, `available()` and
`run_turn(session, folder, prompt, ctl, first) -> TurnResult`. Add it to `harnesses/__init__.py` or list it
as `"module:Class"` in `harness_plugins`. A harness that runs code must declare `sandboxed = True` or it is
not loaded.

### Projects, worktrees and results

- A project is a git repository (branch `main`, committer `SnapLab <snaplab@localhost>`) with `BRIEF.md`
  (goal, done-when, rules; edited by people), `AGENTS.md`, and `STATE.md` (sessions and their last results;
  written only by SnapLab and kept out of git).
- Every session gets a worktree `<project>/.worktrees/<session>` on branch `snaplab/<session>`. The first
  Codex instruction is wrapped in a fixed brief (task, where to look, done-when, what to finish with).
- After each turn SnapLab removes any secret value from the worktree (exit guard), commits the changes on the
  session branch, measures them against the starting commit (`git diff --stat/--numstat`), runs the tests in the
  sandbox, and writes `result.json` (status, summary, changed files, +/- lines, tests, time, tokens). The room
  that started the session receives a **result card** (under 300 tokens) — the bot gets the card, not the
  transcript. When a bot started the session, that bot then tells the user the outcome in a few lines (what
  changed, tests, the next step; `sessions.report_by_bot`, on by default). *Merge into main* makes a merge
  commit; conflicts abort the merge.
- What the phone shows of a session is shortened first: the session folder becomes `.`, the project folder
  its name, SnapLab's data folder `<data>`, the home folder `~`, and the shell wrapper around Codex commands
  (`"C:\…\powershell.exe" -Command "…"`) is dropped, so neither the Windows user name nor long paths appear.
- The project page shows the BRIEF and lets the user edit it (the change is committed on main).

### Proposal cards

Bots propose, the user approves on a card: adding a bot, scheduling a task and starting a session always;
steering a session the user stopped always; and, in a turn where the bot read web content, sending a follow-up,
creating a project, changing the dashboard, saving memory, rewriting a BRIEF or handing a project over. With the
WSL or SSH backend, starting a session, creating a project, changing the dashboard, rewriting a BRIEF and sending
to a session on that backend need no card by default. Configurable (`approvals` in config.json: always /
after_web / never). A card is answered exactly once. The card carries a title key, labelled fields (project, how,
instruction, ...) and the reason it was asked, so it reads the same in every language; the answer is stored as
`Approve`/`Decline` and the note that follows is a translated line, not a chat bubble.

### Memory

Each bot keeps short notes in `memory.md` (4,000 characters); "remember ..." appends a dated line, "forget ..."
removes the lines that contain the words, and a write past the limit is refused with a request to tidy up first.
The lead bot also keeps `shared/user.md` (1,400 characters), which every bot reads; the user edits it under
Settings → About you, and a bot's notes on its page (list with Forget, Add a note, or edit as text). Every change
is also appended to `daily/<date>.md`, which is never loaded into a model call.

### Model slots and priority

All model calls share `slots.count` lock files (2 by default; point `slots.dir` at a shared folder to share the
limit with other tools). Inside SnapLab waiters queue by priority — the user's chat first, then answers and
hand-offs, then session work — and session work may hold at most `slots.session_max` (2) slots so a chat is never
stuck behind sessions. A waiting reply shows "waiting for a free slot · #n".

### What a bot saw

Each chat call stores a snapshot of its assembled context (`data/views/<message>.json`: the prompt sections and
the history with token estimates). The eye button on a bot message opens it.

## Codex and Pi through the translator

Codex and Pi speak only the Responses API; the Snapscribe server speaks only Chat Completions. SnapLab runs a
translator inside its own process. Codex and Pi authenticate to it with a random token, never the key; the
translator adds the real key upstream and masks it in every request.

In the WSL workspace (and with the SSH backend) each job gets its own translator listener and token, the agent
runs inside Bubblewrap (see [local-wsl.md](local-wsl.md)), and the newest two screenshots it opens are passed to
the model as images.

With the Windows backend (`codex.backend: "local"`), every session gets its own `CODEX_HOME`
(`data/codex-homes/<session>/`) with a generated `config.toml`: model provider = the translator, a permission
profile on Windows `mxc` (read and write only the worktree, no command network; see security.md), no built-in web
search, no `view_image`, no apps/plugins/sub-agents, and an empty private git config so git works without reading
`~/.gitconfig`.

## Phone app

`web/` holds `index.html`, `app.js` (all screens), `app.css` (light and dark themes), `i18n/` (every string,
one file per language), `sw.js` (service worker: caches only the app shell, shows push notifications),
`manifest.webmanifest` and icons generated from the sailboat artwork (`tools/make_icons.py`).
Screens: first run (language → lead bot name and colour → how it talks and what to call you → editable prompt),
Home, Chats, a room, Projects, a project, a session, Dashboard, Bots, a bot, Settings, and the Needs-you inbox
(top-bar pill, sheet with answerable cards, and a dashboard card).

## Context budgets (measured)

`config.json` → `context`, measured with the Snapscribe model (first token for a new context: ~2.6 s at 16K,
~6 s at 32K, ~12 s at 64K; quality held to 64K): `target_tokens` 16000, `summarize_tokens` 24000 (the room summary
is updated and old tool results are shortened past this), `max_tokens` 32000 for chats and rooms,
`session_max_tokens` 64000 for background sessions. The builder estimates tokens without a tokenizer and corrects
the estimate per room with the server's own count of the first call (it was ~20% low for short Japanese and
~10% high for long English contexts). Replace the whole builder with `context_builder: "module:Class"`.

## Group rooms

- All members may contribute without a mention; repeated text is hidden and autonomous turns are bounded by
  `bot_conversations`.
- Instructions to sessions from a group room wait for the user's OK on a card, except for sessions in the WSL
  workspace or on an SSH worker.
- A bot reply that names the user and ends with a question is marked "needs you", because bots sometimes asked
  in the text instead of using ask_user.

## Room summaries and the memory tidy

See `snaplab/background.py`: the summary of each room is updated after `summary.every_messages` new messages
(or `summary.recent_tokens`); the memory tidy runs once a day after `memory_tidy.hour`. Both wait behind every
chat for a slot (lowest priority, at most `slots.background_max` at a time).

## Connection band

`App.status()` reports the real state of what the phone depends on, without calling the model: the phone's
own connection (SSE), the model (key present, and the result of the last real call: ok, busy, unreachable,
wrong key, ...), the shared slots (in use, waiting), the Codex translator, web search (paused after a CAPTCHA)
and running sessions. A watcher thread checks every 3 seconds and publishes `status.updated` when anything
changes. The phone shows a coloured dot in the header (green, amber, red) and, when something is down, a band
under the header; tapping either opens the full list.

## Handing a project to a teammate

`write_brief` replaces an owned project's BRIEF.md (committed on main); the lead can edit every project.
The lead's `assign_project` changes the owner, posts a note in that bot's chat and starts its turn.
The teammate's context carries the BRIEF of every project it owns (`context.brief_tokens`, 1200 by default),
so its first answer is a plan based on the brief. Both tools wait for approval after a web read
(`approvals.write_brief`, `approvals.assign_project`). `max_bots` caps the number of bots when it is set
(no limit by default).

## Extension points

Providers and models, harnesses, tools, languages and the context builder: see
[extending.md](extending.md).

## Texts in the user's language

The server's own texts (activity lines, notes after approvals, proposal cards, notifications, the welcome
message, the default persona) come from the same language files as the phone (`snaplab/i18n.py`). Lines that
are stored keep their key and values next to the text, so the phone shows them in its current language.

## Navigation, roles, memory in three layers, SnapCode steps, scheduled tasks

**Navigation.** The tab bar has Chats, Home and Dashboard (see "Home, chat management and SnapCode web research"
below). Projects, SnapCode and the bot list open from the dashboard; a bot's inside screen opens from the name at
the top of its chat. The chat list ends with New group and New bot.

**Roles.** The lead sees every bot, project and memory (`read_memory`). Any bot can create and own several
projects; assignment keeps its other projects. Shared projects are read-only. Tools, context and `archive.Scope`
enforce current access, and only owners or the lead may mutate a project or its SnapCode.

**Memory in three layers.**

| Layer | What | Where | In every context? |
|---|---|---|---|
| 1. Raw archive | everything as it happened, kept for good, append-only | `data/archive/<YYYY-MM>/<day>.jsonl` | no |
| 2. Summaries and memory | room summaries, project STATE, each bot's memory, about you | `data/rooms`, `STATE.md`, `data/bots/<id>/memory.md`, `data/shared/user.md` | yes |
| 3. Search | the `recall` tool; the phone's record search | `data/archive/index.sqlite3` (rebuildable) | on demand |

Records carry `kind` (message, tool, page, work, change, doc, memory), `bot`, `project`, `room`, `session`, `ts`
and `ref` (where to open it). The index is SQLite FTS5 with the trigram tokenizer; terms shorter than three
characters are matched with LIKE; a Japanese phrase without spaces is retried in pieces between particles. When
there are more candidates than wanted, the model orders them (one short call with the "followup" priority;
`recall.rerank`). Recalled web-derived text counts as a current-turn web read. Other untrusted records remain
wrapped but do not trigger the web-read approval rule. The index can be replaced with `recall.index` (same methods as
`FtsIndex`), for example to add meaning-based search with embeddings.

An older data folder is moved forward on start (`snaplab/migrate.py`, `data/schema.json`): files are copied to
`data/backup/migrate-<version>-<time>/` before they change, and the archive is filled once from what is there.

**SnapCode steps.** `.snaplab/PLAN.md` in the work folder is a checklist (`- [ ]` / `- [x]`). Codex and Pi are
asked to write and tick it; the Bot-only harness has `update_plan`. SnapLab reads it every few seconds while the
work runs and after each turn (`SessionManager.refresh_plan`), stores it on the session and shows done/total and
the current step. A project's progress adds up the steps of its open SnapCode. The lead's context lists each
SnapCode with its steps, so it can report progress when asked.

**Scheduled tasks.** `snaplab/tasks.py`: daily, weekly, every N minutes, or once (a wake-up a bot sets for itself
with `schedule_wakeup`). A thread in the server checks every 20 seconds; a run posts a note into the bot's own chat
and queues the bot's turn with the "session" priority (behind chats). Push only when the bot asks the user on a
card.

**Look.** Light, Dark or Match phone (Settings → Appearance, `settings.theme`). No text under 14px; body 16px.
Icons are one SVG file each in `web/icons/ui/`, put into the page as a sprite by the server (`snaplab/icons.py`).

## Faces and icon styles

`snaplab/avatars.py` reads the bundled catalog from `web/avatars/avatars.json`, chooses initial faces by
role, and validates uploaded image containers. A bot's optional `avatar` field is a catalog id, an upload
id, or an empty string for initials. Existing bots without the field keep their initials; no migration is
needed. Initial onboarding uses the captain (`c1`); teammates use role examples from the crew set.

The browser crops an image on a 256 × 256 canvas and posts its PNG pixels as base64 JSON to
`POST /api/avatars`. The original file and EXIF never enter that request. Files live in
`<data_dir>/avatars/`, named by a SHA-256 digest and the detected format. `GET /api/avatars/<id>` uses both
authentication gates and `Cache-Control: no-store`; the service worker does not cache `/api/` responses.
Uploaded files are retained when a face is changed, including uploads from a subsequently cancelled form.

`settings.icon_style` is `line` (default) or `soft3d`. The UI keeps the SVG sprite and adds PNG images from
`web/icons/soft3d/` and its `dark/` folder. CSS selects the icon style and the current light/dark variant;
small status and metadata marks retain SVG. Settings changes apply to already-open sheets as well.

## Context state and provenance

Schema 3 adds `context/state.json`, a program-owned projection of projects, SnapCode, approvals and scheduled
tasks. Canonical saves produce immutable `state` archive records with versions, scope and supersession links.
`StateLedger.concise()` returns bounded current observations and evidence without calling a model.
The builder puts this changing state in the latest message; the stable system prefix stays reusable.

`facts.py` adds explicit source-backed memory entries. The archive index projects effective/recorded times
and replacement relationships, and indexes entity names and aliases separately from original text.
Bot recall defaults to `current`; `historical` requires `as_of`, and `known_at` limits knowledge time.
Archive browsing and direct ID reads retain history. Existing Markdown notes remain compatible.

Tests carry before/after artifact fingerprints, exit codes and an archive evidence ID. Old test results
do not validate later revisions. Approval cards bind the action and target version and are consumed once.
Legacy cards remain readable but cannot authorize an operation without a new proposal.

Tool compaction retains archive references and call/result pairing. Summaries have fixed fields, revisions
and exact message coverage; their JSON is authoritative and SUMMARY.md is a readable mirror. Generation
does not cover later arrivals, and concurrent edits invalidate the pending result. Corrected covered
messages trigger rebuilding from originals. Monitoring and embeddings are not implemented.

## Home, chat management and SnapCode web research

The phone's tabs are Chats, Home (the default) and Dashboard. Home shows the decision count,
horizontal active-work cards with bot faces and progress rings, a two-day scheduled-task timeline,
and small API charts. Dashboard disclosures start closed and contain projects, all active work,
all other work/history, scheduled tasks, API numbers, the team, the board and recent activity.
The bootstrap returns all session summaries.
Task occurrences are a read-only projection in the PC scheduler's timezone, shown in the phone's timezone;
daily/weekly tasks include today's time, while intervals start at the stored next run.

`rooms.json` remains schema 3. Optional `pinned` and `deleted` fields default to false/absent.
`POST /api/rooms/<id>` updates a group name/members or a room's pin. Confirmed group deletion stops
its queued/current replies, cancels open cards and hides it from normal room reads. The room metadata,
message logs, summaries and append-only archive stay on disk. All mutations use the existing paired-device
and same-origin gates. Removed members are rechecked before a queued bot turn starts.
Changes are archived as `room` records. Direct chats are renamed through the bot profile.

Sheets share a pointer-driven grip, scrim dismissal, close button and Escape handling. They trap focus,
make the background inert, and lock root scrolling with `overscroll-behavior: none` while open.
The same code runs in a browser and in the installed app.

The visible name of background work is **SnapCode** in every language. Internal session IDs, tool names and
file formats still say "session".

`web_mcp.py` serves SnapCode's MCP endpoint on the translator's loopback port, at `/mcp/<session>`: `web_search`,
`read_page`, and the external MCP tools that may run unattended (see extending.md). It implements stateless
Streamable HTTP with JSON-RPC initialize, tools/list, tools/call and ping. A per-turn bearer capability is passed
only in the agent process environment, never the config file or sandbox shell. Calls stop being authorized after
Stop, completion or failure. The generated Codex config adds this required MCP server; on the Windows backend
command networking remains disabled.

The bridge reuses `Toolbox` and the existing `Searcher`/page reader, including address, redirect,
peer, CAPTCHA and size checks. Known secrets in arguments are rejected before dispatch; returned web
material stays wrapped as untrusted. Tool calls/results and full bounded page text are archived with
the session/project scope. Page snapshots use session-specific keys. Codex is instructed on every turn
to maintain `.snaplab/KNOWLEDGE.md` with sources, dates and uncertainty. After secret removal, SnapLab
archives changed snapshots (up to 1 MB) as untrusted documents, even when Git is unavailable.
The knowledge file stays in the session worktree and is excluded from Git with the rest of `.snaplab`.

## Project ownership and bot conversations

`access.py` resolves current project ownership from `project.bot`. Every bot can create projects and owns
what it creates; a bot can own several projects. `App.set_owner` moves only the selected project. The lead
can manage all projects and reassign them. `share_project` lets the owner or lead grant/revoke another bot's
read-only access through optional `project.shared_with`. Read and write checks differ: shared projects expose
their BRIEF, sessions and project-scoped archive records but cannot be modified or run by the reader. The
same checks apply to tools, context, the inside screen and approval consumption. Project-scoped archive
records require current project access even if the reader authored them or belongs to their original room.

`Toolbox` exists for one bot turn. Successful external reads also persist their bot, turn and source under
the current origin in `bot-conversations.json`. Later turns inherit those reads across bots and rooms;
already-running turns refresh them before tool dispatch. A fresh human message starts an unmarked origin.
Failed requests and internal-only recalled tool results do not mark it. Successful untrusted plugin results
are archived with `web_material: true`; legacy records are recognized by the current plugin declaration.
Proposal metadata distinguishes direct reads from inherited reads. The explanation names the source bot
in the final message, inbox card and push payload. Retrying retains the failed turn's read provenance.
Bot-only sessions and SnapCode's web MCP calls use a persistent `session:<id>` origin. Their reads
include the session id, title and turn number. Automatic result reports copy these reads into the report
origin before enqueueing, so both the reporter and later bots require approval. The reason names the
session. Session follow-ups retain these reads because their context can still contain earlier results.

`message_bot` sends into a visible group shared by sender and recipient, or creates/reuses a two-bot group.
`create_group` includes its creator, invites other bots and posts an opening message. Both leave a link in
the source room. There is no private bot mailbox. Delivery and subsequent replies use the existing room
queue and `followup` slot priority, behind the user's chat, through the configured model registry and shared
OS lock files (`slots.count`). All group members can contribute or return `[SILENT]`;
mentions determine order. Follow-ups continue until everyone stays silent, a question needs the user, or a
configured limit applies. A direct address to the configured user name combined with a clear English or
Japanese reply/approval wait sets `needs_you` even without a question mark. That origin waits for the user;
already queued turns also skip it. A new human message starts a fresh origin. Text is withheld from group
streaming until duplicate checking finishes.

`dialogue.py` persists counters, recent content fingerprints and stopped conversations in
`bot-conversations.json`, under the store lock. One originating human message retains its budget when a bot
sends to another room or creates a group. `bot_conversations.max_consecutive` defaults to 6 attempts across
the conversation, `max_hourly` (attempts per bot in a rolling hour across groups) to 0, meaning no limit, and
`repeat_window` to 20 recent normalized texts per conversation. Silent/error turns and outgoing tool messages
count before execution. One-to-one user replies do not consume these limits. Limits do not automatically resume
work: the user sends another message. Stop text or the Stop button cancels connected queued/running turns and
persists the stopped origin; new messages can start a new origin, never revive an old one. New Bot groups
carry `bot_only` until a user speaks. `bots_paused`, `created_by`, `bot_pair`, `origin`, `to_bots` and
`bot_conversation` are optional metadata. Schema 3 files remain directly readable; no destructive migration
is needed. The version-1 importer preserves multiple existing assignments.
