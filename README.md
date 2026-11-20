<p align="center"><img src="web/logo.png" width="100" alt="SnapLab"></p>

# SnapLab

SnapLab runs on your home PC and puts a team of AI bots in your phone. You chat with them from anywhere; they
remember what matters, search the web, run tasks on a schedule, use the tools you connect, and hand long jobs
(coding, testing, operating a browser) to **SnapCode**, a coding agent that works in an isolated Linux workspace
on the same PC.

SnapLab is made for Snapscribe subscribers. It needs only your Snapscribe API key and
your PC: the model is **Snapscribe 1**, and everything else runs at home.

> **Status:** early. Windows has a one-click installer; Linux and macOS install with a script (see below). The Mac
> version has not been tried on a real Mac yet.

## What you need

- A Windows 10/11 PC that stays on while you use SnapLab (it does the work; your phone is the screen).
- A Snapscribe API key from the Snapscribe website.
- For phone access: a free [Tailscale](https://tailscale.com) account on the PC and the phone.
- For SnapCode: virtualization enabled in the PC's firmware (WSL 2 is set up for you).

## Install on Windows

1. Download **[SnapLab-Setup.exe](https://github.com/So-Zes/snaplab/releases/latest/download/SnapLab-Setup.exe)**
   and open it. The file is not code-signed yet, so Windows SmartScreen may say it "protected your PC": choose
   **More info**, then **Run anyway**.
2. Setup explains what it installs and why it needs administrator permission once: turning on WSL and installing
   Python and Git change Windows itself. SnapLab itself always runs as you. If everything is already there, it
   does not ask.
3. It downloads SnapLab, prepares the private Linux workspace for SnapCode (the longest step, a few minutes), adds
   SnapLab to the Start menu and to "start when I sign in", and opens SnapLab in your browser. If Windows has to
   restart to finish turning on WSL, setup continues by itself after you sign in again.
4. In the browser, paste your Snapscribe API key, name your first bot, and follow the short tour. SnapLab checks
   the key with one small request and keeps it in Windows Credential Manager, never in a file.

To open SnapLab later, choose **SnapLab** in the Start menu. To remove it, use **Settings > Apps > Installed apps >
SnapLab** (your conversations stay unless you tick the box to delete them). Data lives in `~\.snaplab\data` and the
app in `~\.snaplab\app`. Details, options and how the installer is tested: [docs/installer.md](docs/installer.md).

Prefer a script? [install.ps1](install.ps1) does the same without the window (`-SetupWorker` adds the workspace).

## Install on Linux

```sh
curl -fsSL https://raw.githubusercontent.com/So-Zes/snaplab/main/install.sh -o install.sh
sh install.sh
```

Debian or Ubuntu with systemd. The script installs what is missing, asks whether to set up the SnapCode workspace
(a separate `snaplab` account on the same PC, reached over SSH from the PC only), registers a systemd user service
and an app menu entry, and opens SnapLab in the browser. Details: [docs/linux.md](docs/linux.md).

## Install on a Mac

The same two lines in Terminal (Homebrew required). SnapCode's workspace is a small Ubuntu VM made with Lima;
SnapLab starts at login through a LaunchAgent. Not yet tested on a real Mac. Details: [docs/mac.md](docs/mac.md).

## Use it from your phone

SnapLab listens only on `127.0.0.1`. Phone access goes through your own Tailscale network with `tailscale serve`
(never Funnel, so SnapLab is not on the public internet), and each phone is paired once with a one-time code.

The tour after the first start offers **Set up** or **Not now** (later: **Settings > Phone access**). Set up walks
through four steps on the PC: Tailscale is installed if needed (Windows asks for permission), you sign in to
Tailscale, you allow a private HTTPS address once if your tailnet has never used one, and a QR code appears. Install
Tailscale on the phone, sign in with the same account, scan the code and tap **Pair**. Only the owner of the
tailnet, on a paired device, gets in.

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

Tools: **Settings > Tools (MCP servers)** adds MCP servers (paste the JSON from a server's README, or a command
or URL); bots and SnapCode (Pi and Codex) get their tools at once.

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
