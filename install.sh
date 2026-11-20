#!/bin/sh
# SnapLab for Linux and macOS. Download this file and run:  sh install.sh
# Or from a checkout:  sh install.sh
#
# Settings (environment variables, all optional):
#   SNAPLAB_INSTALL_DIR  where the app goes          (default ~/.snaplab/app)
#   SNAPLAB_DATA         where your data goes        (default ~/.snaplab/data)
#   SNAPLAB_PORT         the local port              (default 18445)
#   SNAPLAB_WORKER       yes / no: set up the SnapCode workspace on Linux (asks when not set)
#   SNAPLAB_NO_BROWSER   1: do not open the browser at the end (servers without a screen)
set -eu
trap 'echo "SnapLab setup stopped. Check the message above and run the same command again. Existing settings were kept." >&2' 0
install_dir=${SNAPLAB_INSTALL_DIR:-"$HOME/.snaplab/app"}
data_dir=${SNAPLAB_DATA:-"$HOME/.snaplab/data"}
repository=${SNAPLAB_REPOSITORY:-https://github.com/So-Zes/snaplab.git}
port=${SNAPLAB_PORT:-18445}
os=$(uname -s)
echo 'Checking Python 3.11+ and Git. Existing installations are not upgraded.'
python_ok() { command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)'; }
if ! python_ok || ! command -v git >/dev/null 2>&1; then
    if command -v brew >/dev/null 2>&1; then
        if ! python_ok; then echo 'Installing Python 3.13 with Homebrew.'; brew install python@3.13; fi
        if ! command -v git >/dev/null 2>&1; then echo 'Installing Git with Homebrew.'; brew install git; fi
        if ! python_ok; then PATH="$(brew --prefix python@3.13)/libexec/bin:$PATH"; export PATH; fi
    elif command -v apt-get >/dev/null 2>&1; then
        echo 'Installing Python and Git with apt. Your system may ask for your password (sudo).'
        sudo apt-get update
        if ! python_ok; then sudo apt-get install -y python3 python3-venv; fi
        if ! command -v git >/dev/null 2>&1; then sudo apt-get install -y git; fi
    elif [ "$os" = Darwin ]; then
        echo 'Install Homebrew from https://brew.sh (it brings Git), then run this command again.' >&2
        exit 1
    else
        echo 'Install Python 3.11 or newer and Git with your package manager, then run this command again.' >&2
        exit 1
    fi
fi
python_ok || { echo 'Python 3.11 or newer is required. Install it and run this command again.' >&2; exit 1; }
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -f "$script_dir/tools/launch.py" ]; then install_dir=$script_dir
elif [ ! -e "$install_dir" ]; then
    echo "Downloading SnapLab into $install_dir."
    git clone -- "$repository" "$install_dir"
elif [ -d "$install_dir/.git" ]; then
    echo "Updating SnapLab in $install_dir."
    git -C "$install_dir" pull --ff-only || echo 'Could not update; keeping the version that is there.'
elif [ ! -f "$install_dir/tools/launch.py" ]; then
    echo 'The installation folder already contains other files. Choose another SNAPLAB_INSTALL_DIR.' >&2; exit 1
fi
if [ ! -x "$install_dir/.venv/bin/python" ]; then
    echo "Creating a Python environment in $install_dir/.venv."
    python3 -m venv "$install_dir/.venv" || {
        if command -v apt-get >/dev/null 2>&1; then
            sudo apt-get install -y python3-venv && python3 -m venv "$install_dir/.venv"
        else false; fi
    } || { echo 'Install the python3-venv package for this Python version and run this command again.' >&2; exit 1; }
fi
venv_python="$install_dir/.venv/bin/python"
if ! "$venv_python" -c 'import cryptography, keyring' 2>/dev/null; then
    echo "Installing cryptography and keyring in $install_dir/.venv."
    "$venv_python" -m pip install --disable-pip-version-check cryptography keyring
fi

# The SnapCode workspace on Linux: a separate "snaplab" account reached over SSH from this PC only.
if [ "$os" = Linux ]; then
    answer=${SNAPLAB_WORKER:-}
    if [ -z "$answer" ] && [ -r /dev/tty ]; then
        printf '%s' 'Set up the SnapCode workspace now? It adds a separate "snaplab" account and about 3 GB, and needs sudo. [Y/n] '
        read answer < /dev/tty || answer=y
    fi
    case "$answer" in
        n|N|no|NO) echo 'Skipping the SnapCode workspace. Run this installer again with SNAPLAB_WORKER=yes to add it later.' ;;
        *)
            wdir="$HOME/.snaplab/linux-worker"
            mkdir -p "$wdir"; chmod 700 "$wdir"
            command -v ssh-keygen >/dev/null 2>&1 || sudo apt-get install -y openssh-client
            [ -f "$wdir/identity" ] || ssh-keygen -q -t ed25519 -N '' -C snaplab-worker -f "$wdir/identity"
            if sudo sh "$install_dir/tools/setup_linux_worker.sh" "$wdir/identity.pub"; then
                ssh-keyscan -T 10 -t ed25519 127.0.0.1 2>/dev/null > "$wdir/known_hosts"
                if ssh -F /dev/null -i "$wdir/identity" -o IdentitiesOnly=yes -o UserKnownHostsFile="$wdir/known_hosts" \
                       -o GlobalKnownHostsFile=/dev/null -o StrictHostKeyChecking=yes -o BatchMode=yes -p 22 snaplab@127.0.0.1 true; then
                    printf '{"ssh_port": 22}\n' > "$wdir/ready.json"
                    echo 'SnapCode workspace: ready.'
                else
                    echo 'SnapCode workspace: SSH to the snaplab account did not work, so SnapCode stays off. Chat works.' >&2
                fi
            else
                echo 'SnapCode workspace: setup did not finish, so SnapCode stays off. Chat works.' >&2
            fi ;;
    esac
fi

# Start with the session (systemd user service on Linux, a LaunchAgent on macOS) and an app menu entry.
if [ "$os" = Linux ] && command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    unit_dir="$HOME/.config/systemd/user"
    mkdir -p "$unit_dir"
    cat > "$unit_dir/snaplab.service" <<EOF
[Unit]
Description=SnapLab
After=network-online.target

[Service]
ExecStart="$venv_python" "$install_dir/tools/launch.py" --data "$data_dir" --port $port --foreground
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable snaplab.service >/dev/null 2>&1 || true
    systemctl --user restart snaplab.service || true
    echo 'SnapLab starts by itself when you sign in (systemd user service "snaplab").'
fi
if [ "$os" = Linux ]; then
    apps="$HOME/.local/share/applications"
    mkdir -p "$apps"
    cat > "$apps/snaplab.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=SnapLab
Comment=Open SnapLab in the browser
Exec="$venv_python" "$install_dir/tools/launch.py" --data "$data_dir" --port $port
Icon=$install_dir/web/icons/icon-512.png
Terminal=false
Categories=Utility;Network;
EOF
fi
if [ "$os" = Darwin ]; then
    agents="$HOME/Library/LaunchAgents"
    mkdir -p "$agents"
    cat > "$agents/com.snaplab.app.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.snaplab.app</string>
  <key>ProgramArguments</key><array>
    <string>$venv_python</string><string>$install_dir/tools/launch.py</string>
    <string>--data</string><string>$data_dir</string><string>--port</string><string>$port</string><string>--foreground</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
</dict></plist>
EOF
    launchctl bootout "gui/$(id -u)/com.snaplab.app" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$agents/com.snaplab.app.plist" 2>/dev/null || true
    echo 'SnapLab starts by itself when you log in (LaunchAgent com.snaplab.app).'
fi

browser=""
[ "${SNAPLAB_NO_BROWSER:-}" = 1 ] && browser="--no-browser"
"$venv_python" "$install_dir/tools/launch.py" --data "$data_dir" --port "$port" $browser
trap - 0
