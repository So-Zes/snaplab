# Changelog

Newest first. Each entry: the date, what changed, and why.

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
