# SnapLab on Linux

```sh
curl -fsSL https://raw.githubusercontent.com/So-Zes/snaplab/main/install.sh -o install.sh
sh install.sh
```

The script installs Python 3.11+ and Git if they are missing (apt; it asks for your password), downloads SnapLab
into `~/.snaplab/app`, creates its Python environment, and asks whether to set up the SnapCode workspace. Then it
registers a systemd user service (`snaplab`, starts when you sign in), adds SnapLab to the app menu, and opens
SnapLab in the browser. Paste your Snapscribe API key, name your first bot, and follow the short tour.

Settings: `SNAPLAB_WORKER=yes|no` answers the workspace question, `SNAPLAB_NO_BROWSER=1` skips the browser (a server
without a screen), `SNAPLAB_PORT`, `SNAPLAB_DATA` and `SNAPLAB_INSTALL_DIR` move things.

## The SnapCode workspace

On Windows SnapCode works in a dedicated WSL distribution. On Linux it works in a separate, locked account named
`snaplab` on the same PC (`tools/setup_linux_worker.sh`, run with sudo by the installer):

- Debian or Ubuntu with systemd. The script installs OpenSSH, Bubblewrap, Python, Node.js, the desktop tools,
  Pi, Codex and Google Chrome (x86-64) for that account, and limits the memory its jobs may use.
- SnapLab reaches the account over SSH with its own key (`~/.snaplab/linux-worker/identity`) and a pinned host key.
  `authorized_keys` accepts that key from 127.0.0.1 only and allows nothing but the port forward SnapLab needs.
- Each job runs inside Bubblewrap in that account, as on the WSL worker. If Ubuntu's AppArmor setting forbids the
  namespaces Bubblewrap needs, the script allows them for `/usr/bin/bwrap` only.
- Other distributions: create a locked `snaplab` account, install the packages listed at the top of
  `tools/setup_worker.sh`, then follow the SSH part of `tools/setup_linux_worker.sh` by hand and write
  `{"ssh_port": 22}` to `~/.snaplab/linux-worker/ready.json`.

Without the workspace SnapLab still chats, remembers, searches the web, runs scheduled tasks and uses MCP servers;
only SnapCode is off.

## API keys

Keys go to the Secret Service (GNOME Keyring, KWallet) when one is running. A PC without one (a server, a minimal
desktop) keeps them in `~/.snaplab/secrets.json`, readable only by you (mode 0600), the way command-line tools keep
their tokens.

## Phone access

Install Tailscale and let your account manage it once:

```sh
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale set --operator=$USER
```

Then use **Settings > Phone access > Set up** as on Windows: sign in, allow HTTPS once if asked, and scan the QR
code with the phone.

A server without a screen: forward the port to a computer that has a browser (`ssh -L 18445:127.0.0.1:18445 you@server`)
and open the one-use link that `tools/launch.py` prints there, then set up phone access from it.

## How it was tested

On a fresh Ubuntu 24.04 (a WSL distribution with systemd, standing in for a Linux PC): the installer from GitHub,
the workspace setup, the systemd service, the setup page, chat with the real Snapscribe model, a SnapCode job in the
`snaplab` account, and an MCP server added in Settings. Not tested: a desktop session's app menu and Secret Service,
Tailscale on Linux, and distributions other than Ubuntu.
