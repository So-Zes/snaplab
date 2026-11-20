<p align="center"><img src="web/logo.png" width="100" alt="SnapLab"></p>

# SnapLab

SnapLab runs on your home PC and puts a team of AI bots in your phone. You chat with them from anywhere; they
remember what matters, search the web, run tasks on a schedule, use the tools you connect, and hand long jobs
(coding, testing, operating a browser) to **SnapCode**, a coding agent that works in an isolated Linux workspace
on the same PC.

SnapLab is made for Snapscribe subscribers. It needs only your Snapscribe API key and
your PC: the model is **Snapscribe 1**, and everything else runs at home.

> **Status:** early. Windows comes first, then Linux, then macOS. A one-click Windows installer and a first-run
> tutorial are being built; until then, install with the script below.

## What you need

- A Windows 10/11 PC that stays on while you use SnapLab (it does the work; your phone is the screen).
- A Snapscribe API key from the Snapscribe website.
- For phone access: a free [Tailscale](https://tailscale.com) account on the PC and the phone.
- For SnapCode: virtualization enabled in the PC's firmware (WSL 2 is set up for you).

## Install on Windows

Download [install.ps1](install.ps1) to your Downloads folder, then paste this line into PowerShell:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$HOME\Downloads\install.ps1" -SetupWorker
```

The script installs what is missing (Python 3.11+ and Git, through Windows Package Manager), creates a separate
Python environment for SnapLab, prepares the SnapCode workspace (`-SetupWorker`: a dedicated WSL 2 Linux
distribution, checked for isolation before code work is enabled) and opens SnapLab in your browser. Existing
software is not upgraded. Run the same command again to open SnapLab later; your conversations and settings stay.

In the browser, choose **Snapscribe 1**, paste your API key and name your first bot. SnapLab checks the key with
one small request and keeps it in Windows Credential Manager, never in a file.

Data lives in `~\.snaplab\data` and the app in `~\.snaplab\app` (change them with `-DataDir` and `-InstallDir`).

## Use it from your phone

SnapLab listens only on `127.0.0.1`. Phone access goes through your own Tailscale network with `tailscale serve`
(never Funnel, so SnapLab is not on the public internet), and each phone is paired once with a one-time link.

**Not ready yet in this version:** an installation made with the script above runs in local mode (the PC's
browser only). The guided phone setup — install Tailscale, sign in, scan a QR code on the phone — is the next step
of the work.

## What it does

- **Bots you can talk to.** A lead bot plus teammates, each with a name, colour, persona and long-term memory.
  Chat one to one or in groups (`@Name` asks a bot first; say "stop" to stop a conversation). Replies stream in,
  with the model's thinking folded away. Tap a bot to see exactly what it sees.
- **Memory.** Every conversation, web page read and SnapCode log is kept in a local archive; bots search it with
  `recall`. Short notes per bot and shared notes about you go into every conversation, and you can read and edit
  them. Rooms keep a running summary, and a nightly tidy merges duplicate notes.
- **SnapCode.** Bots start background work sessions in project folders (each project is a git repository; each
  session works on its own branch). SnapLab picks the agent: **Pi** for code, documents and web apps, **Codex** for
  operating the screen. Both run inside the WSL workspace with a private desktop and a real Chrome window, so they
  can test web apps and games, take screenshots and send them to the chat. Several sessions can run at once.
- **Computer use.** SnapCode can click, type and read the screen of its private desktop. See
  [docs/computer-use.md](docs/computer-use.md).
- **Scheduled tasks and wake-ups.** "Every morning at 8, check …" becomes a task of that bot. A bot can also set a
  wake-up to look in on its own work later.
- **External tools (MCP).** Add MCP servers in `config.json` (`mcp_servers`); their tools appear for bots and for
  SnapCode. See [docs/extending.md](docs/extending.md).
- **Files and photos.** Send photos, PDFs and documents from the chat; bots read them, and SnapCode can work on them.
- **Home and dashboard.** Decisions waiting for you, running work, scheduled tasks, API usage, and widgets your bots
  keep up to date.
- **Notifications.** Web Push to the installed app when a bot needs you or work finishes.
- **Protection.** API keys never leave SnapLab's process (SnapCode talks to a local translator with a per-turn
  token), private words such as your home folder and user name are replaced before anything is sent out, and web
  material is treated as untrusted. See [docs/security.md](docs/security.md).

The interface is in English, Japanese and Russian; it follows the phone's language, and English is the default.
Another language is one file in `web/i18n/`.

## Everyday commands

Run these in the app folder (`~\.snaplab\app`) with its Python (`.venv\Scripts\python.exe`):

```powershell
python snaplab.py status          # server, tailscale serve, logon task, devices, key present?
python snaplab.py pair --label phone
python snaplab.py devices         # paired devices and unused links
python snaplab.py revoke d-xxxxxx # revoke a device (p-... for a link, "all" for everything)
python snaplab.py stop            # stop the server (start again with: python snaplab.py start)
python snaplab.py reset --yes     # move the data folder aside and start over with first-run setup
python snaplab.py check-secrets   # confirm the API key value is not inside any repository file
```

## Settings

Most people never need `config.json`. It is ignored by git and holds machine-specific values; everything has a
default ([config.example.json](config.example.json) shows the common keys). Model providers, MCP servers,
approvals and limits are described in [docs/extending.md](docs/extending.md) and [docs/architecture.md](docs/architecture.md).

If the Snapscribe address or model changes, nothing needs reinstalling: choose **Use a different address** on the setup
page, or set `SNAPLAB_SNAPSCRIBE_URL` / `SNAPLAB_SNAPSCRIBE_MODEL`, or add `"presets": {"snapscribe": {"base_url": "..."}}`
to `config.json`. The defaults live in [snaplab/presets.py](snaplab/presets.py).

## Development

```powershell
python -m unittest discover -s tests          # unit and integration tests, no API calls
python tools/demo_server.py --data runtime/demo --port 18446   # the UI with a scripted model (no API calls)
node tests/round9_ui.mjs                      # dependency-free UI checks (see tests/*_ui.mjs)
python tools/codemap.py                       # rebuild docs/CODEMAP.md after adding or removing files
```

Start with [docs/MAP.md](docs/MAP.md) (how the parts fit together) and [docs/CODEMAP.md](docs/CODEMAP.md) (one line
per file). Changes are recorded in [docs/CHANGELOG.md](docs/CHANGELOG.md).

## License

SnapLab is free for personal use, noncommercial organizations and small businesses (PolyForm Noncommercial or
PolyForm Small Business, your choice). For other uses, open an issue to ask. See [LICENSE.md](LICENSE.md).
