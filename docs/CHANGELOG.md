# Changelog

Newest first. Each entry: the date, what changed, and why.

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
