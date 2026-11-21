# SnapLab security and privacy

## Reaching the app

- The server binds to 127.0.0.1 only (enforced in code). `tailscale serve` publishes it inside the tailnet
  over HTTPS. Funnel requests are refused.
- Gate 1: the `Tailscale-User-Login` header must equal the tailnet owner's login (read from
  `tailscale status --json`). If the owner is unknown, nobody gets in.
- Gate 2: device pairing. `python snaplab.py pair` prints a one-time link valid for 24 hours. The token is
  consumed only when the button on the page is pressed (link previews cannot use it). The device receives
  an HttpOnly, Secure, SameSite=Lax `__Host-` cookie (about 400 days, renewed on use). `auth.json` stores only
  SHA-256 digests. Devices and links can be listed and revoked from the command line.
- An installation made by the installer starts in local mode: the server answers only requests from this PC
  addressed to `127.0.0.1:<port>`, refuses forwarded and Tailscale headers, and pairs the PC's browser through a
  one-use local link. Phone access through Tailscale uses the two gates above.
- State-changing requests must be JSON, carry `X-SnapLab: 1`, and come from the app's own origin.
  Responses carry a strict Content-Security-Policy (`script-src 'self'`), `nosniff`, `no-referrer`.
- Static files come from a fixed table; URLs never become file paths.
- **Known limit — other services on the same name.** Cookies are not separated by port, so the browser also
  sends the pairing cookie to other services published under the same `<pc>.<tailnet>.ts.net` name (other
  `tailscale serve` ports). Only run services you trust under that name, or give SnapLab a name of its own
  (for example a separate Tailscale node or Tailscale Services). Anything running on the PC itself could also
  send the owner header to 127.0.0.1 directly; SnapLab trusts the PC it runs on.

## Code execution

Bots have no tool that runs code. Code runs only in SnapCode sessions. Installations made by the installer run
SnapCode in the dedicated WSL workspace (`codex.backend: "wsl"`); its boundary (separate namespaces, Bubblewrap,
seccomp, public-only web access through a broker, checks before every task) is described in
[local-wsl.md](local-wsl.md). The SSH backend uses the same job sandbox on a Linux machine. The rest of this
section covers the Windows `mxc` sandbox used with `codex.backend: "local"` and by the Run tests harness.

### What a Codex session can see in the Windows sandbox

Every session gets:

- **Its own git worktree** (`<project>/.worktrees/<session>`, branch `snaplab/<session>`), so the project's
  main folder stays clean until you press *Merge into main*.
- **Its own Codex home** (`data/codex-homes/<session>/`) with a generated *permission profile* on the Windows
  `mxc` sandbox:

  | | allowed | denied |
  |---|---|---|
  | read | the worktree; the minimal system files Codex needs; the Python install; the project's `.git` (read only) | everything else: home folder, documents, `~/.gitconfig`, `~/.ssh`, SnapLab's data folder, other projects, temp folders |
  | write | the worktree | everywhere else, including `.git` (SnapLab commits after each turn) |
  | network | none | all |

  This is tested for real (`tests/test_sandbox.py`: a neighbouring file, the project's main folder, the home
  folder, `~/.gitconfig` and SnapLab's data folder are unreadable; writing outside fails; Python, git and
  tests work; the network is off; no key or proxy token in the environment).
- SnapLab refuses to start Codex if the generated config lacks any of these settings, or if a legacy
  `sandbox_mode`, a bypass flag or an extra writable folder appears in the config or command line (a legacy
  `sandbox_mode` would make Codex ignore the permission profile).
- Codex's `view_image` tool (which reads image files from the Codex process itself, outside the sandbox) is
  switched off.
- Tests run in the same sandbox, never directly on the PC.

**Known limit — legacy registry copies.** The `mxc` sandbox does not restrict reading the Windows registry, and
`setx SNAPSCRIBE_API_KEY …` stores the key in `HKCU\Environment`. A command inside the sandbox can therefore
read the key value. What protects it:

1. Codex never gets the key: its process environment has no variables named like keys or tokens, and it talks
   to SnapLab's translator with a random per-process token, not the key.
2. The translator replaces the key value in every request before it goes to the model, so the model never
   sees it even if a command printed it.
3. The sandbox has no network, so a command cannot send it anywhere itself.
4. **Exit guard** (`snaplab/guard.py`): everything SnapLab stores or sends is compared with the key value and
   the value is replaced — chat messages and thinking, result cards, session timelines and results, files a
   session changed (cleaned before SnapLab commits, so commits never contain it), the session's Codex
   transcript files, the dashboard, notifications, the event log, and every response and live event sent to
   the phone.

The guard also catches the usual encodings of each key (base64, URL-safe base64, hex, reversed). It does **not**
catch every change a command could make (letters split by spaces, the value cut in pieces, other encodings).
If you add providers (docs/extending.md) and keep their keys in the registry with `setx`, a session can read
those keys too, and a changed form could reach the model of another provider. For more than one provider,
keep keys out of the registry: prefer Windows Credential Manager, verify that the deployed sandbox cannot
read those credentials, and remove old registry copies. Running sessions as a separate Windows user is
another isolation option. Command networking in the Windows sandbox remains disabled after moving the key.

## The API key

- **Windows Credential Manager is the recommended storage location.** Use a generic credential whose
  target exactly matches `api_key_env`, for example `SNAPSCRIBE_API_KEY`. SnapLab reads it for the Windows
  account running the application, using `ctypes` and `advapi32.CredReadW`; no extra package is required.
- Authentication checks a nonempty process environment value first, then the generic credential, then
  `HKCU\Environment` for compatibility. Empty, missing or inaccessible credentials fall through without
  printing their contents. Non-Windows hosts skip the Windows sources. Generic passwords use UTF-16LE,
  as entered through Credential Manager; arbitrary binary blobs are not supported.
- The exit guard and MCP argument checks read **all three current values**, not just the chosen key, at
  each check. Rotating a credential does not hide it from the guard when an old process value still exists.
  Previously observed keys remain protected for the lifetime of the guard.
- SnapLab never writes these values to its data files, logs, responses or repository. Windows manages
  the persisted credential; this does not mean the credential is accessible only to SnapLab.
- `python snaplab.py check-secrets` refuses when a repository file contains the key value (it can also run from
  a git pre-commit hook).
- With several providers, each one names its own environment variable (`providers.<name>.api_key_env`).
  Every such value is added to the exit guard, and only names, never values, are kept in the config or shown
  on the phone ("key set" / "no key").

To register a key without exposing it in console output, process arguments or PowerShell history:

```powershell
control.exe /name Microsoft.CredentialManager
```

1. Open **Windows Credentials**, then **Add a generic credential**.
2. Enter the configured environment variable name, such as `SNAPSCRIBE_API_KEY`, in the Internet or
   network address field. This is the credential target, not the provider URL.
3. Set the user name to `SnapLab` and enter the key in the masked password field. Save the credential.
   SnapLab uses only the target and password. Do this under the account that runs SnapLab.
4. When migrating, first verify credential access from SnapLab's trusted process without printing the
   value. After that verification, remove the old user environment entry and this terminal's inherited copy:

   ```powershell
   [Environment]::SetEnvironmentVariable('SNAPSCRIBE_API_KEY', $null, 'User')
   Remove-Item -LiteralPath Env:SNAPSCRIBE_API_KEY -ErrorAction SilentlyContinue
   ```

5. Restart SnapLab from a process without the old inherited variable, using the same Windows account.
   If other tools used the old environment entry, update their configuration as part of the migration.
   Rotate keys previously exposed through the registry when appropriate.

In testing, the current `mxc` sandbox was denied access to the credential. Recheck that boundary with a
synthetic credential after deployment; never print a real key to test it. The offline test runner replaces
`CredReadW` and `CredFree` with in-memory implementations, replaces the registry and clears secret environment
variables, so tests do not read the real credential store. Use `python tools/test_offline.py` for that suite.

The native interface follows Microsoft's [CredReadW documentation](https://learn.microsoft.com/en-us/windows/win32/api/wincred/nf-wincred-credreadw),
[CREDENTIALW layout](https://learn.microsoft.com/en-us/windows/win32/api/wincred/ns-wincred-credentialw) and
[CredFree documentation](https://learn.microsoft.com/en-us/windows/win32/api/wincred/nf-wincred-credfree).

## Bot actions and approvals

- Proposal cards: a bot proposes, the user approves on a card (never from chat text). By default a bot needs
  approval to add a bot, to schedule a task, to start a session, to steer a session the user stopped, and — in a
  turn where it read web content — to send a follow-up, create a project, change the dashboard, save memory,
  rewrite a project's BRIEF, change project sharing, contact another bot, create a group or hand a project to a
  bot. With the WSL or SSH backend, starting a session, creating a project, changing the dashboard, rewriting a
  BRIEF and sending to a session on that backend need no card by default.
  Configurable in `config.json` → `approvals`.
- Tool plugins declare their own approval (`never`, `after_web`, `always`); `approvals.<tool name>` in the
  config overrides it. Plugins run inside the SnapLab process with its permissions, so only add code you
  trust; the shipped example (`calculator`) evaluates arithmetic without `eval` and runs no code.
- A harness that runs code must declare `sandboxed = True` and is expected to run every command through the
  session sandbox (`sandbox_run`); one that declares `runs_code` without it is not loaded. The contract check
  cannot prove that a harness really uses the sandbox, so treat a new harness like any code you install.
- A card is answered exactly once (concurrent answers are refused). Cards show the full instruction.

## Web content

- `web_search` and `read_page` results are wrapped as untrusted material; the house rules tell bots never to
  follow instructions inside them.
- `read_page` fetches http(s) only, refuses local, private, link-local and tailnet addresses, every address of
  the PC itself (looked up live, public IPv6 included, with its IPv6 /64), and IPv4 addresses inside IPv6 ones
  (mapped, compatible, NAT64, 6to4, Teredo) (checked at every
  redirect), ignores system proxies, and checks the address it actually connected to (so a name that resolves
  differently the second time cannot reach the PC or the home network). Text types only, capped size.
- A search site or page that answers with a human check (CAPTCHA) instead of content counts as no answer; search
  goes on to the next source. A source that says its limit is used up is paused for its Retry-After.

## Guarded web research for SnapCode

In the Windows sandbox, command networking stays disabled. In the WSL workspace, commands reach public web servers
only through the broker described in [local-wsl.md](local-wsl.md). In both, the agent process (Codex or Pi) can
reach a narrowly scoped MCP endpoint on SnapLab's loopback translator: `/mcp/<session>`. It offers `web_search`,
`read_page` and the external MCP tools that may run unattended (see extending.md); no file, shell, arbitrary
HTTP, tool-plugin or resource tools are exposed. The server enforces that list independently of the client config.

Each turn receives a random bearer capability in `SNAPLAB_WEB_TOKEN`; the value is not written to config
or disk. The shell environment excludes tokens and keys. The capability belongs to one session and is
revoked on completion/failure; Stop also denies calls immediately through the turn's cancellation flag.
A web request already in progress may finish under the existing fetch timeout. Browser Origins and
non-loopback Host headers are refused. Requests are capped at 32 KiB; JSON notifications cannot execute tools.
The endpoint uses stateless Streamable HTTP with JSON responses and no inbound SSE stream.

Before dispatch, all arguments are checked by the existing exit guard against every configured model
provider's key, Brave/Tavily keys, explicit extra secret variables and active capabilities. Process environment,
Windows Credential Manager and legacy registry values are all refreshed without logging them, even when
authentication selects a different source. Previously observed values remain guarded too. Raw values,
the guard's base64/hex/reversed variants, and up to
four layers of percent encoding are checked. A match rejects the whole call, rather than sending a redacted
URL. Invalid calls are not archived with their arguments. This is not protection against arbitrary encodings,
splitting a secret across requests, or a hostile agent inventing a new encoding; the existing registry threat
has not disappeared. Keep commands in the Windows sandbox offline and do not treat MCP as a complete
data-loss-prevention system.

Successful calls reuse the existing public-address/redirect/connected-peer checks, content-type and size
limits and private-term redaction. Web text is wrapped as reference material and the
Codex instructions explicitly prohibit following instructions inside it. This instruction is not a proof that
a model will resist every prompt injection. The filesystem sandbox remains the execution boundary.
Tool results and pages are archived in the session's scope; knowledge snapshots are marked untrusted and
pass through secret removal before archiving. They do not authorize work or become verified memory facts.

Before enabling command networking in the Windows sandbox in a future change:

1. Remove API keys from registry-backed user/machine environment values readable by the sandbox, and
   rotate keys that were exposed there. Removing only an inherited process environment variable is insufficient.
2. Use Windows Credential Manager for SnapLab's keys and verify that the deployed sandbox cannot read it,
   or run sessions as a separate
   Windows account/isolated VM that cannot read the owner's registry, credentials, processes or files.
3. Verify the separation with real sandbox tests (registry, process environment, filesystem and child
   processes), using synthetic canaries and no real secret output.
4. Define explicit destination/package-registry policy, proxy/redirect/DNS rules, size/rate limits, and a
   separate user-approved installation path. Do not reuse the current broad registry permissions with open egress.
5. Retest all channels, including MCP arguments, translator inputs, logs, files, Git and notifications.
   Encoding detection alone is not sufficient to justify enabling the network.

## Privacy

Before any request leaves the PC (to the model or a search engine), `privacy.py` replaces the home folder path,
the Windows user name, the computer name, the tailnet host name and owner login, and any `private_terms` from
config with placeholders. The home-folder placeholder in tool-call arguments is mapped back for local tools only.
Conversations, sessions and keys stay in the git-ignored data folder.

## Dashboard

Bots write widgets through `update_dashboard`, which accepts six data shapes only; every field is checked
(types, lengths, counts) and unknown fields are rejected. The phone renders text with `textContent`; no HTML
or script from a bot is ever inserted.

## Notifications

Web Push payloads are encrypted (RFC 8291) and signed with VAPID (RFC 8292). Only https endpoints of known
browser push services are accepted. Three kinds (needs you, ready for review, failed), each can be switched
off, and the same notice is sent once.

## What the phone shows of the PC

Session timelines, last replies and errors pass through a display step before they are stored or sent:
paths inside the session folder become relative, the project folder becomes its name, SnapLab's data folder
`<data>`, the home folder `~`, and the private terms (user name, computer name, tailnet name) are replaced as
for the model. Command lines lose their shell wrapper. The full output stays on the PC in the worktree.

## Memory

Memory is plain Markdown in the data folder: each bot's notes, the shared notes about the user and a daily log
of changes. The exit guard cleans every write, so a secret value is never saved there. Bots are told not to
save secrets, and writes after a web read wait for approval.

## Who sees what, the record, scheduled tasks, computer use

- **Who sees what is checked in the tools.** A teammate can create projects, manage all its own projects and
  read explicitly shared projects. `recall` checks current project access first for project-tagged records;
  other records follow authorship and room membership, also for direct ID reads. The lead sees everything.
- **The raw archive is kept for good:** nothing removes it automatically. It lives in the data folder next to
  the conversations, with secret values removed by the guard before writing, like every other file. Deleting a
  memory note in the app does not delete its earlier versions from the archive. Web text in it stays untrusted:
  recalled pages, tool output and SnapCode logs remain wrapped. Only explicitly retrieved web-derived content
  triggers the current-turn web approval rule; internal-only output does not.
- **Scheduled tasks** are made only through a card the user approves (`approvals.schedule_task`, default
  `always`), belong to one bot, and can be paused or deleted from the phone. A one-time wake-up that a bot sets
  for itself (`schedule_wakeup`) runs once in the chat where it was set.
- **Computer use** happens only on SnapCode's private Linux desktop inside the job sandbox, never on the user's
  own Windows desktop; see [computer-use.md](computer-use.md).

## Project permissions and bot conversations

All bots can create and own several projects, and since 2026-11-21 every bot may read and change every project
and its sessions (before, only the owner and lead could change one, and `shared_with` granted reading only).
Ownership and shares now only decide which projects a bot's context shows. Rooms are unchanged: a bot's recall
still covers the rooms it is in, not other bots' one-to-one conversations. Messages already
received in a shared room remain part of that room's visible conversation; sharing cannot erase knowledge.

Successful external reads and explicit recall of web-derived material mark the current human-origin
conversation. The mark and source bot/turn survive restart and follow group replies, cross-room contact,
group creation and assignment. Later Toolboxes inherit it; tool dispatch refreshes it for concurrent rooms.
A new human message starts fresh. Retry retains read provenance without reopening the stopped origin.
Failed reads, internal-only tool records, history text and reads from unrelated origins do not trigger it.
Untrusted plugin results carry `web_material: true`; legacy successful records are recognized by the current
plugin's `untrusted` declaration, never by a generic `meta.untrusted` flag alone. Untrusted wrappers remain
intact. The server distinguishes direct and inherited reads in the chat, approval card and push payload;
browser push still depends on device permissions and delivery.
Bot-only sessions and SnapCode's web MCP tools persist successful reads under the session's origin,
including the session id, title and turn. Workspace jobs (WSL or SSH) have internet access, so each of their
turns also marks the session's origin as having read external material. Automatic result reports inherit these
reads before the reporting bot starts, and subsequent bot turns retain them. Approval reasons identify the
session that read web material. Failed reads and other sessions do not contribute; session follow-ups retain
their own earlier reads.

Bots communicate only in user-visible groups. `message_bot` checks membership when an existing room is
specified; new groups include their creator. These tools and `share_project` use `after_web` by default.
Peer messages are reference material, not authorization from the user. Bot-to-bot messages add no network or
command execution capability. Bot calls use the existing shared locks and lower `followup` priority.

Cross-room conversation limits are reserved atomically before model turns and outbound messages. Persistent
rolling-hour counts and stopped origins survive restart. Creating another group never resets an origin's
budget. Normalized repeated text is suppressed; paraphrases are bounded by the consecutive limit (and the hourly
limit when one is set). A human Stop cancels connected active/queued turns and denies late tool calls. Explicit
new human input starts a new conversation; an old stopped origin cannot be revived by a bot. Defaults are in
[extending.md](extending.md) (Tools for bots).
Clear statements addressed to the configured user name while waiting for their reply or approval also
stop automatic turns in that origin, including already queued turns. This is a conservative English/Japanese
text check, supplemented by prompt instructions to announce a wait once and avoid repeating other bots.

Group deletion is a hide operation: original conversations remain. Old clients must not be used to edit
the same data concurrently because they do not understand the optional deletion field.

## Provenance and authorization

Approval cards bind an action hash and target revision. A stale, expired, legacy-unbound or consumed card
cannot authorize an operation. The proposer’s current project access is rechecked before consumption.
Failed execution still consumes the card; a retry requires a new proposal. Retrieved approval text and
summary snapshots never authorize execution.

Source-backed facts inherit the exact permissions of their sources; mixed scopes and widening to shared
memory are refused. External material cannot establish a verified fact, and conjectures cannot replace
verified facts. Derived untrusted material remains wrapped when recalled. Current/historical filters
apply before candidate limits; direct archive ID access still checks current permissions.

All archive metadata, state, test evidence and summary manifests pass through the exit guard. Originals
are append-only; supersession changes the disposable index projection. `all` retrieval still exposes
authorized history, including forgotten Markdown versions. This is not a complete deletion or global
do-not-use feature. The synthetic evaluation runner does not load the live config or application data.

## Uploaded faces

- Uploads use the existing paired-device, tailnet-owner, JSON, `X-SnapLab` and same-origin checks,
  including during first-run setup. Image retrieval also requires both authentication gates.
- The browser sends only a new 256 × 256 PNG made by canvas. It does not upload the source photo,
  filename or EXIF. A local source is limited to 20 MiB and 64 million pixels.
- The server accepts PNG, JPEG and WebP signatures, at most 1 MiB decoded, with each dimension from
  1 to 1024 pixels. PNG CRCs, chunk boundaries and bounded decompression are checked. PNG input is
  restricted to non-interlaced 8-bit images, as produced by canvas. JPEG and WebP container/frame
  headers are checked; they are not fully decoded by the server. Animated WebP/PNG are rejected.
- The upload route alone has a 1,400,000-byte JSON request limit; other routes keep their existing limit.
  The generated digest id is strictly validated before it is used as a filename. Clients cannot supply a
  path or URL. Files stay under `<data_dir>/avatars/` and are not repository assets.
- Uploaded images are served with their detected MIME type, `nosniff` and `no-store`. They are excluded
  from service-worker caching. A revoked pairing cannot fetch them again. Images already visible on a
  device stay visible until that page is replaced, as with other displayed app content.
