# Changelog

Newest first. Each entry: the date, what changed, and why.

## 2026-11-22 Version 1.1.5

- Version 1.1.5: an experimental decision watch (off by default) that wakes a bot when work shows no progress.

## 2026-11-22 Experiment: a decision model watches work without progress and wakes a bot

- `decision_watch.py` (new): while switched on, it looks every 2 minutes at running SnapCode sessions and at bot
  replies being written. Work without progress for 10 minutes is described (kind, title, how long it has run and
  been quiet, its last few events with a short excerpt) to a decision model with one yes/no question: is it stuck
  in a way that should be looked at now? At 0.6 or more a bot is woken like a wake-up it set itself: a session's
  own bot in the room it was started from; for a stuck reply the lead bot in its own chat (or a push notification
  when the lead's own reply is stuck). Nothing is stopped or cut. The same work is not asked about again for 30
  minutes.
- The model is `liquid/d1.2-ultra` on OpenRouter's decisions endpoint (`/api/alpha/decisions`), key
  `OPENROUTER_API_KEY` in the credential store. What is sent goes through the same redaction as model calls (home
  folder, user and PC names, tailnet) and the API key guard; OpenRouter's decision models may keep what they
  receive. Every decision is appended to `data/decision-watch.jsonl`.
- On and off: Settings > Experiments (`decision_watch` in settings.json) or `decision_watch.enabled` in config.json
  (the setting wins); off by default. `every_s` 120, `quiet_s` 600, `wake_at` 0.6, `rewake_s` 1800, `model`, `url`
  and `key_env` can be changed in config `decision_watch`.
- Easy to take out: the feature is this one module plus one start in `server.background()`, one view in the
  bootstrap, `POST /api/decision-watch` and one card in Settings. Switching it off stops the next check and any
  decision call still waiting.
- Why: SnapLab no longer cuts waiting work by time, so something has to notice work that hangs. Tried with the
  real model on two synthetic descriptions: a page wait quiet for 41 minutes got 0.99, a Rust build quiet for 10
  minutes 0.28; one call costs about $0.000015.

## 2026-11-22 Version 1.1.4

- Version 1.1.4: a project's `data/`, `runtime/` and `config.json` now move between the PC and the worker.

## 2026-11-22 Project data folders come back from the worker

- `remote_worker.SKIP` no longer contains `data`, `runtime` and `config.json`. They had been added with the SSH
  worker to keep SnapLab's own private folders out of it, so a project's own data folder or config file never
  came back from SnapCode. Credentials (`.ssh`, `.aws`, `.config`, `.env*`, `credentials*`, `auth.json`, `.npmrc`,
  `.netrc` and the like) are still neither sent nor returned, and files that hold a secret or a private term are
  still stopped by their content.

## 2026-11-21 Version 1.1.3

- Version 1.1.3: the rules, caps and timeouts that had been added without the owner asking are gone (all below).
  Bots and SnapCode have no default limits on bot-to-bot replies, message or memory length, reply length or waits
  (a wait ends with Stop), no approval cards unless the settings ask for them, every bot has every tool and every
  project, and new projects no longer tell SnapCode that its workspace is offline. Running the installer again
  updates an existing install.

## 2026-11-21 The usage rules, caps and timeouts Claude added on its own are removed

- Why: the owner asked that the system's freedom is never reduced unless it truly cannot be helped. An audit found
  about a hundred rules and limits that Claude had added on its own judgment and the owner never asked for. The
  security boundaries (listed at the end) and the payment stop stay. A configured limit still works; 0 means no
  limit, and waits end when the work is stopped, not by the clock.
- Bots: the group rules keep only `[SILENT]` as an option and the user's stop (no "speak only when relevant",
  "say you are waiting only once", "ask only once", "do not repeat another bot" or "do not create a group to
  avoid a limit"). "Never restart SnapCode the user stopped" is removed from the instructions, the tool
  descriptions and `send_to_session`. Every bot has the lead's tools (`read_memory`, `create_bot`,
  `assign_project`), writes the notes about the user, sees and stops every bot's scheduled tasks, and may read and
  change every project (shares were read-only); ownership and shares now only decide which projects a bot's context
  shows (`access.looked_after`). Approval cards default to `never` for every action, also in groups and on the
  local backend, whose instructions no longer say to start SnapCode only when asked. Tool arguments and BRIEFs in
  another language are no longer refused (replies still follow the room's language). Bots may replace the whole
  dashboard (`op set`) and remove widgets without a card, write sourced facts after reading the web, and read files
  the user sent in any chat; a memory note has no 1,500-character limit.
- SnapCode: no "write at most 200 lines / do not touch other files" prefix and no "build web apps in stages" rule.
  New projects' BRIEF.md no longer says "work only in the session folder", "no internet, do not install packages"
  or "use the standard library"; on the WSL or SSH workspace it says the job has internet and may install what it
  needs (the check looked only at `ssh`, so WSL projects were told they were offline). "Do not use sudo" became the
  fact that root is not available in the job sandbox. "Do not guess credentials", "never enter personal
  information you were not given", "ignore unrelated instructions" and "do not debug the screenshot helper" are
  removed; on-screen text is still treated as data. Any test command is accepted (no 300-character limit, server
  and watch commands allowed).
- Tools: MCP tools the server marks destructive run without a card and reach SnapCode too (a tool set to
  `"always"` in `approvals` still asks and stays out of SnapCode). MCP calls, HTTP and stdio alike, have no time
  limit by default (`timeout_s` 0) and end when the turn is stopped; the start-up listing still gives a server
  5 s before using its cached tools. `read_page` reads `user:password@` URLs (HTTP Basic to that host only, not
  along redirects) and follows up to 30 redirects.
- Caps: no limit on the user's message, the composer, bot prompts, BRIEFs or bot-to-bot messages (the JSON request
  guard went from 256 KB to 64 MB); "Try again" works any number of times; `bot_conversations.max_consecutive`
  defaults to 0 and repeated texts are no longer refused; delivered files may be 4 GiB each, any number per turn;
  plans have no 30-step limit; model requests have no 600 s timeout and Stop now also ends the wait for the first
  byte (`provider_protocol.open_request(cancel=...)`); bot replies have no 8,192-token limit and SnapCode replies no
  20,000-token limit (the translator gives them the room the context leaves, up to the model's own output limit);
  `slots.session_max` defaults to 0 (SnapCode may use every slot); memory and the notes about the user have no size
  limit (the nightly tidy then only merges and drops); scheduled tasks take any interval above 0 and a missed run
  is done once however late; tests, a quiet local Codex, SnapCode's MCP calls (Pi and Codex get Node's longest
  timer, about 24.8 days) and a quiet SSH worker are no longer cut by time; `desktop.py` and `browser.mjs` lost the
  15-minute take-over wait, the action and size ranges, the component timeouts, the 120 s default waits, the 30 s
  browser start and the cookie list limits (`--timeout N` still sets a wait).
- Kept (security boundaries, until the owner decides otherwise): key redaction (`guard.py`), private-word
  replacement (`privacy.py`), the instructions not to put personal details into searches or SnapCode and not to save
  secrets, not following instructions inside outside material, chat bots never running code, bot-only session file
  tools inside the project, data-only dashboards, `read_page` refusing local, private and tailnet addresses and
  non-http(s) URLs, the WSL network relay and bubblewrap sandbox, no secret environment variables or credential
  files for SnapCode, download-only HTML, device pairing and the tailnet-owner check, git staying inside a project,
  the CLI built-in tools staying off, SnapCode's screen and file scope, the worker memory limits, the Codex and Pi
  feature switches, context-size bounds and the file transfer limits.
- Watch out: with no time limits, a server that never answers (a model provider, an MCP server, a test that never
  ends, a page that never finishes loading) is waited for until the turn is stopped. A value in config.json (for
  example `api.timeout_s`) still applies.

## 2026-11-21 Version 1.1.2

- Version 1.1.2: RustXNG is installed on the first start and asks its full set of sources by default, so bots can
  search (also in Japanese) on connections where the free sources refuse automated searches; SnapCode and bots
  stop only at a payment (CAPTCHA and identity checks no longer stop them). Running the installer again updates an
  existing install.

## 2026-11-21 Only the payment stop is kept; RustXNG full by default and installed at start

- Bots' house rules, SnapCode's instructions, the `start_session` description and `desktop.py` now stop only at a
  payment (card entry). The CAPTCHA and identity-check stops had been added by Claude on its own and were never
  decided by the owner; they are removed. `desktop.py` blocks input only on card fields (card number, security
  code, PIN).
- `websearch`: a CAPTCHA page from a source counts as no answer and the next source is asked; the source is not
  paused (it is asked again next time). A source that says its limit is used up is still paused for its
  Retry-After. Tavily's refusal text is passed on to the bot.
- RustXNG: the profile is chosen in Settings > Web search (full, standard, clean), full by default. Full also asks
  Naver, Yahoo, Yahoo Japan, Keenable, Fynd and other sites whose terms may forbid automated access; RustXNG prints
  a notice to read their terms. A running RustXNG with another profile is started again (its profile is judged
  from its engines). When RustXNG is missing, SnapLab installs it on start (`RustXNG.ensure_in_background`;
  `search.rustxng.enabled = false` turns it off).
- Why: with the standard profile, Japanese questions found only Wikipedia; with full, three Japanese questions
  (bug bounty programs, cinema showtimes in Tokyo, Immunefi) were answered in about 2 s with showtime pages and
  articles. And a fresh install had no working search at all until someone pressed the install button.

## 2026-11-21 Version 1.1.1

- Version 1.1.1: web search tries its sources in order and takes a Tavily or Brave Search key in Settings, RustXNG
  can be installed as a last source that never refuses, SnapCode no longer stops on a linked folder in its
  workspace, and removing an installation removes only the WSL workspace it created (all below, since 1.1.0).
  Running the installer again updates an existing install. Why: on connections that share one public address with
  many people, bots could not search at all.

## 2026-11-21 SnapCode: a linked folder in the workspace is skipped, not fatal

- `remote_worker.inventory()` no longer raises "linked workspace directory": a linked folder (symlink or junction)
  is not entered, nothing in it is returned to the PC, and its name is collected. At the end of a turn the worker
  sends the names (`linked`) and the PC notes in the session that they stay on the worker. Sync still refuses to
  write through a linked folder (`write_own`, `through_link`), so nothing is written to a link's target.

Why: a bot cloned a repository with one symlinked folder (`packages/contracts-test/contracts`), and every turn of
that session failed with "SSH job or file return failed". Checked on the real workspace (6,994 files): the new
inventory skipped the one linked folder and finished in 0.4 s.

## 2026-11-21 Web search: RustXNG as the last source

- `rustxng.py`: RustXNG (github.com/RustXNG/RustXNG, a Rust rewrite of SearXNG, AGPL-3.0) can be installed from
  Settings > Web search. The official v0.32.0 release for this platform is downloaded from GitHub and checked against
  the SHA-256 written in `rustxng.py` (a mismatch is deleted), kept in `~/.snaplab/rustxng/v0.32.0/`, and started on
  127.0.0.1:29483 with its `standard` profile, which asks only providers that allow automated use (Mwmbl, Wikipedia,
  Wikidata, wiby; GitHub, Stack Overflow, arXiv and others for their topics). SnapLab starts it again on every start
  (after the deploy check) or uses one that already answers. Nothing is downloaded without the button.
- `websearch`: `rustxng` is the last source of `auto` while it runs; the bot is told that it searched open indexes
  with small coverage of current and Japanese pages. `search.provider` `rustxng` uses only it.
- `App.search_view`: a failure while building the search state no longer breaks `/api/bootstrap`.

Why: with DuckDuckGo and Tavily keyless both refusing on a shared address, bots had no search at all. RustXNG never
refuses and needs no key; its index is small, so it comes after the others. Checked on a real PC: the button
downloaded the release, the SHA-256 matched, RustXNG started and answered, and a search that DuckDuckGo and Tavily
refused was answered by RustXNG.

## 2026-11-21 Web search: sources tried in order, a search key in Settings

- With `search.provider` `auto`, `websearch.Searcher` tries Tavily (key), Brave Search (key), DuckDuckGo and then
  Tavily's keyless access (`X-Tavily-Access-Mode: keyless`, offered by Tavily for agents) and uses the first that
  answers. A source that answers with a human check or a used-up limit is paused for its `Retry-After` (otherwise
  `cooldown_minutes`) and the next one is tried; when all are paused the bot is told when search comes back and
  that a key avoids it. The text of a refusal (it offers paid workarounds) is not passed to the model.
- Settings > Web search (`searchCard`, `POST /api/search`, `setup.search_key`) shows the sources and which is in
  use or paused, and takes a Tavily or Brave Search key: it is tested with one search, then kept in the OS
  credential store as `TAVILY_API_KEY` / `BRAVE_API_KEY` and added to the redaction list. It can be removed again.
- The status item for search is "paused" only when every source is paused.
- `read_page` says a name that does not resolve was not found, instead of calling it local, private or tailnet.

Why: on a PC whose connection shares one public address with many people (mobile and 5G home routers),
DuckDuckGo answered every search with a human check and Tavily's keyless access was already at its daily limit,
so bots could not search at all. A key of the user's own (Tavily: 1,000 searches a month free, no card) does not
depend on the connection, and before this there was no place in the app to enter one.

## 2026-11-21 Installer: removing an installation removes only the workspace it created

- Setup records in `~\.snaplab\workspace-<name>.txt` whether it created the WSL distribution, found it already
  there, or skipped it (`--skip-wsl`); removal unregisters the distribution only when this installation created it.
  Installations from 1.1.0 or earlier have no record, and then only the default installation removes it. Why:
  removing a second, test installation made with `--name` and `--skip-wsl` unregistered `SnapLab-Worker`, the
  workspace of the SnapLab already running on that PC, because removal looked only at the default folder's
  `setup-result.json`. Checked with the built installer: a `--skip-wsl` install and removal left the workspace in
  place, a named installation without a record left it in place, and a `created` record removed it.

## 2026-11-21 Settings: Remove only for models added there

- Settings > Models offers Remove only for models added in Settings. Models from config.json and the defaults
  (`config_models`, recorded by config.load) cannot be removed there. Why: adding a model copies the whole list into
  provider.json, so those models looked removable and came back after a restart.

## 2026-11-21 Version 1.1.0

- Version 1.1.0: models in Settings (make one the default, remove one), Google Gemini, Claude Code and the Codex
  CLI as models under Advanced settings, the browser panel, the memory fixes, the hybrid recall index and memory
  lane 1 (all below, since 1.0.0). Running the installer again updates an existing install and adds the optional
  packages for search by meaning. Why: each part was tried end to end with a real model.

## 2026-11-21 Recall: hybrid index by default

- `recall` (and memory lane 1) now searches with HybridIndex (`hybrid_index.py`): records are cut into ~350-token
  chunks; a trigram word search, a CJK bigram word search (finds 1-2 character Japanese words) and a search by meaning
  (multilingual-e5-small, int8 ONNX on the CPU, `embedder.py`) are fused by rank, with a small bonus for the share of
  the query's words found. Why: measured on a copy of a real archive (16,154 records, 243 questions with hand-checked
  answers), the right record was in the top 10 for 50% of keyword queries (was 45%) and 48% of natural questions
  (was 26%), and a query took 24 ms at the median (was 96 ms). Larger models (ruri-v3) gained 3-5 points more but
  need PyTorch; a cross-encoder reranker cost about a second per query; both stay out.
- The meaning model (135 MB) is downloaded once from a pinned revision and checked by SHA-256. The installers add
  numpy, onnxruntime and tokenizers as an optional step: without them, or without the download, recall searches by
  words only. "recall": {"semantic": false} turns the meaning part off.
- Chunks and vectors are made by a background thread that waits while a bot turn runs (about 30 minutes for 16,000
  records on one core); until they are ready the old whole-record search answers, and a restart resumes the work.
  The bot's record card shows how far it is.
- Each hit carries `coverage`, `content_coverage` and `dense`. content_coverage counts only the words that say what
  the query is about (no English function words, no kana-only pieces) and matches English as whole words.

## 2026-11-21 Memory lane 1: automatic recall

- Before each reply SnapLab looks up the latest message in the archive and puts up to 3 trusted, clearly relevant
  records from earlier into the latest message (at most 8% of the call's budget), with their dates, places and
  record ids. Web pages, tool output and worker events never come in this way, and nothing the bot already sees
  (the visible history, this room's summary) is repeated. Why: the archive reached the model only when a bot called
  recall, and small models often do not; research on long-term memory found that a short block of retrieved text at
  the end of the prompt helps more than a longer history (LongMemEval, Lost in the Middle).
- A record comes in when at least 60% of the message's content words are in it (English function words and
  kana-only pieces do not count) or its meaning is close (cosine 0.88 with multilingual-e5-small). Calibrated on
  243 questions about a copy of a real archive (16,154 records) and 35 everyday requests: no everyday request
  got a record. The first guess (any words, 50%) gave records to 9 of the 35 ("What is the capital of
  Australia?" matched "what", "is", "the", "of").
- Measured with Snapscribe 1, each arm on its own copy of the archive: 40 questions and 15 everyday requests.
  Snapscribe 1 called recall itself for 37 of the 40, so the lane added records to 13. On those 13, 10 answers
  were right with the lane and 9 without, with 30% fewer recall calls and a median reply of 121 s instead of
  194 s. Overall 25 and 27 of 40 were right: on the 27 questions where the lane added nothing, 8 answers flipped
  between the two runs anyway, so the difference is run-to-run variation. 14 and 15 of the 15 everyday replies
  stayed on topic; the one miss had no record added. The lane stays on: it costs no model call, and models that
  call recall less often than Snapscribe 1 get the most from it.

## 2026-11-21 Memory fixes

- Notes keep their newest lines in the context. Why: notes grow at the end, but the fixed 800-token budget kept
  the beginning, so a Japanese note showed about 1,000 of its 4,000 characters and the newest lines were the ones
  cut (the user note: about 520 of 1,400). Lines that do not fit become one marker line that says recall finds them.
- Budgets follow the model's window (`context.budgets`). With a 128K window a whole note, user note and summary fit;
  with 16K-32K the messages leave room for the tool list, the reply and the provider's margin. Why: on 16K models a
  full context plus the tool list was larger than the provider check allows, and old tool results were shortened
  only past a fixed 24,000 tokens, which small windows never reach. Values set in config.json still win.
- Windows of 16K or less get a shorter tool list: first sentences of the descriptions only (about a quarter
  smaller), with the same names, parameters and required fields.
- Room summaries show next_actions and open_items first and leave out empty fields; identifiers are capped at 40
  (new ones first, earlier ones only while the summary still mentions them) and only open approvals are listed.
  Why: both lists only grew, and the 1,000-token clip removed next_actions, the last field.

## 2026-11-21 Browser panel

- Home has a Browser button next to the work heading: it opens the browser of the running SnapCode session, or
  of the newest one. Why: the panel could only be reached from a session's page.
- A SnapCode session's browser can be opened inside SnapLab, on the PC or the phone ("Open browser" on the session,
  `#/browser/<id>`). The user sees the page live, types an address, clicks, scrolls and types (Japanese IME
  included), switches tabs and answers page dialogs. During a turn it is the agent's own Chrome: the user watches
  and can take over and hand back; between turns SnapLab runs the same Chrome alone in the workspace, so a site
  the user signs in to there is signed in for the agent's next turn (`browser_live.py`, `remote_worker.live`,
  `browser.mjs --live` and `host`). Why: the user can watch the agent and step in for sign-ins and checks that
  need a person, without the browser leaving the sandbox.
- The agent's live browser is now a Chrome window on the job's screen by default; `open` and `do` start it when
  needed. Why: the default was a browser without a window, so the session's screen view stayed black.
- The browser profile restores its tabs, and kept processes get five seconds to exit after SIGTERM, so cookies and
  tabs are written. While the user holds control, `browser.mjs open/do` and `desktop.py` input wait (up to 15
  minutes) and then tell the agent to look again.
- On a phone, pages are laid out for the phone's screen while the user holds the browser. A turn's worker waits up to
  90 s for its job instead of failing when the panel's browser is still closing.

## 2026-11-20 Claude and ChatGPT apps, Google Gemini

- Settings > Advanced settings (closed by default): other API providers, and "AI apps signed in on this PC":
  Claude Code (Claude) and the Codex CLI (ChatGPT). SnapLab shows whether each is installed and signed in, opens the
  official install or sign-in command in a window on Windows (Linux and macOS show the command), tests one short
  turn, and adds the app as a model for bots (`cli_admin.py`, `claude_cli.py`, `codex_cli.py`, `/api/cli`). Why:
  people with a Claude or ChatGPT plan can use it for bots without an API key; it stays optional because
  Snapscribe 1 is the default.
- The programs run unmodified with the user's own sign-in, which SnapLab never reads; their built-in tools are
  off and only SnapLab's tools are offered. Status checks find installs made after SnapLab started (Windows
  registry PATH, ~/.local/bin and the usual folders) and are cached for a minute. Codex features this version does
  not know are left out of the --disable list.
- Google Gemini (API key) on the setup page, through Google's OpenAI-compatible endpoint. Why: Google's terms do
  not allow other apps to use the Gemini CLI sign-in, so a key is the allowed way.

## 2026-11-20 Models in Settings

- Settings > Models: "Use for bots" makes a model the one bots use when they have no model of their own (routing
  `chat`), and "Remove" deletes a model added in Settings; bots that used it go back to the default, and its key is
  deleted when no other model needs it (`setup.set_default`, `setup.remove_model`, `POST /api/models`). Why: models
  could be added but never removed or made the default.

## 2026-11-20 Version 1.0.0

- Version 1.0.0: the first complete release for Windows, Linux and macOS (the Mac version is still untested on a
  real Mac). It contains everything up to here: the Windows installer, the first-run tour and phone access, MCP
  servers from Settings, and the Linux and Mac install scripts. Why: every planned part of the first version is
  built and was tried end to end on Windows and Linux with a real model.

## 2026-11-20 Fixes from the first real installs

- The installers now add Pillow and pypdf to SnapLab's Python environment. Why: without Pillow, photos went to
  the model with their camera metadata (such as the place); without pypdf, bots could not read PDFs.
- Linux and macOS: the server may bind its port again right after a restart (SO_REUSEADDR; Windows keeps it off
  so no other program can take the port). Why: systemd restarts failed with "Address already in use".
- install.sh installs python3-venv on a fresh Debian or Ubuntu and waits for the service before opening
  SnapLab; the Linux workspace setup copies setup_worker.sh to /opt/snaplab so the snaplab account can read it.
- The Windows installer asks for a restart whenever it turned WSL on.
- Five tests that assumed Windows now check the platform, so the suite also runs on Linux.

## 2026-11-20 Mac version

- `install.sh` on macOS: Homebrew Python and Git, the Keychain for keys, a LaunchAgent, and (when asked) the
  SnapCode workspace in an Ubuntu VM made with Lima (`tools/setup_mac_worker.sh`), reached over SSH like the Linux
  worker. Not tried on a real Mac yet; see docs/mac.md.
- `tools/ci/install-and-test.yml`: a GitHub Actions workflow that installs on Windows, Linux and macOS runners. It
  is kept outside .github/workflows because pushing workflows needs a GitHub login with the workflow permission.

## 2026-11-20 Linux version

- `install.sh` sets up the SnapCode workspace on Linux (`tools/setup_linux_worker.sh`: a locked `snaplab` account,
  Bubblewrap, Pi, Codex, Chrome, SSH from 127.0.0.1 only with SnapLab's own key), a systemd user service and an app
  menu entry; on macOS a LaunchAgent. `tools/launch.py --foreground` runs the server for systemd and launchd.
- Keys on a Linux PC without Secret Service go to `~/.snaplab/secrets.json` (0600); macOS still requires the
  Keychain. Why: Linux servers and minimal desktops have no keyring, and setup failed there.
- Phone access on Linux explains the two Tailscale commands (install, `--operator`) when they are needed.

## 2026-11-20 MCP servers from Settings

- Settings > Tools (MCP servers): add a server by pasting its JSON (the Claude Desktop / Claude Code / Cursor
  form), a command line, or a URL; check it, switch chat use on or off, remove it. Each server is started once
  to list its tools before it is saved; secret values go to the OS credential store. Why: users should add the
  MCP servers they already use without editing config.json, and bots, Pi and Codex should all get them.
- stdio MCP servers (`command`/`args`) are supported next to Streamable HTTP. They run on the PC and SnapCode
  reaches them through the snaplab_web bridge. Changes apply without a restart (`app.reload_mcp`).
- The room header and Settings show "Snapscribe 1" instead of the model id behind it.

## 2026-11-20 Windows installer, first-run tour, phone access

- `SnapLab-Setup.exe` (installer/): explains why it needs administrator permission, asks for it only when WSL,
  Python or Git are missing, then installs SnapLab, its Linux workspace, Start menu and sign-in entries as the
  user, and opens SnapLab. It continues after a restart and can remove SnapLab again. Why: subscribers should not
  need PowerShell.
- `.gitattributes` keeps LF in every clone. Why: Git for Windows checked shell scripts out with CRLF, and the
  Linux workspace setup failed on them (found by the first real installer run).
- `tools/setup_wsl.py` says why a Linux step failed; the WSL distribution name and key folder can be changed with
  SNAPLAB_WSL_DISTRO and SNAPLAB_WSL_DIR (a second installation or a test never touches the first).
- `tools/launch.py --no-browser` (used at sign-in), and a message window when the Start menu shortcut fails.
- First-run tour after onboarding, ending with Set up / Not now for phone access.
- Phone access (`phone.py`, Settings > Phone access): Tailscale install, sign-in, `tailscale serve`, and a QR code
  that pairs the phone. Why: using SnapLab from the phone is required, and local installations had no way to do it.

## 2026-11-20 Changeable Snapscribe address, license

- The provider presets (address, model, label) now live in `snaplab/presets.py`, and the setup page reads them
  from the server. config.json and environment variables override them, and the setup page has "Use a different
  address". Why: the Snapscribe address for subscribers may change, and that should not need a new release.
- Added LICENSE.md: PolyForm Noncommercial or PolyForm Small Business, the user's choice. Why: people need
  permission to install and use SnapLab, and starting with these terms keeps later choices open.

## 2026-11-19 First public version

SnapLab for Snapscribe subscribers, split from a private build.

- Included: bots and group chats, memory and the archive search, SnapCode (Pi or Codex, chosen automatically) in a
  local WSL workspace with a private desktop and Chrome, computer use, scheduled tasks and wake-ups, files and
  photos in chat, external MCP servers, the home screen and dashboard, Web Push, and the protections (key guard,
  private-word replacement, untrusted web material).
- Not included: advisor bots that ran on other services' command-line tools, voice input, image generation, and
  research experiments. They depend on services or hardware beyond Snapscribe and the user's PC.
- The default model is shown as "Snapscribe 1", so the model behind it can change without changing the app.
- English is the default language; the pairing pages now use the language files.
