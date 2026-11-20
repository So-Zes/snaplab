# SnapLab on a Mac

```sh
curl -fsSL https://raw.githubusercontent.com/So-Zes/snaplab/main/install.sh -o install.sh
sh install.sh
```

Needs [Homebrew](https://brew.sh). The script installs Python 3.13 and Git with Homebrew if they are missing, downloads
SnapLab into `~/.snaplab/app`, creates its Python environment, asks whether to set up the SnapCode workspace,
registers a LaunchAgent (`com.snaplab.app`, starts when you log in) and opens SnapLab in the browser. Paste your
Snapscribe API key, name your first bot, and follow the short tour. API keys go to the macOS Keychain.

## The SnapCode workspace

macOS cannot run SnapCode's Linux tools directly, so `tools/setup_mac_worker.sh` makes a small Ubuntu virtual
machine with [Lima](https://lima-vm.io) (Apple's Virtualization framework, macOS 13 or later; 4 CPUs, 6 GiB memory,
up to 40 GiB disk, no access to the Mac's files). Inside it, `tools/setup_linux_worker.sh` creates the same locked
`snaplab` account as on Linux. SnapLab reaches it through the SSH port Lima forwards to 127.0.0.1, with its own key
and a pinned host key (`~/.snaplab/mac-worker`).

Stop the VM when you do not need SnapCode: `limactl stop snaplab`. Remove it: `limactl delete snaplab`.

## Phone access

Install Tailscale from the Mac App Store or tailscale.com/download and sign in, then use **Settings > Phone access >
Set up**.

## How it was tested

Not on a real Mac yet: no Mac was available. Checked: the shell scripts' syntax, the parts of SnapLab shared with
Linux (tested on Ubuntu 24.04), and the Mac-specific choices in unit tests. `tools/ci/install-and-test.yml` installs
SnapLab on a GitHub macOS runner once it is turned on (it needs a GitHub login with the "workflow" permission).
