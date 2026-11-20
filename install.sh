#!/bin/sh
# Run from a checkout, or download this file and run: sh install.sh
set -eu
trap 'echo "SnapLab setup stopped. Check the message above and run the same command again. Existing settings were kept." >&2' 0
install_dir=${SNAPLAB_INSTALL_DIR:-"$HOME/.snaplab/app"}
data_dir=${SNAPLAB_DATA:-"$HOME/.snaplab/data"}
repository=${SNAPLAB_REPOSITORY:-https://github.com/So-Zes/snaplab.git}
port=${SNAPLAB_PORT:-18445}
echo 'Checking Python 3.11+, Git and optional Node.js/Codex. Existing installations will not be upgraded.'
python_ok() { command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)'; }
if ! python_ok || ! command -v git >/dev/null 2>&1; then
    if command -v brew >/dev/null 2>&1; then
        if ! python_ok; then echo 'Installing Python 3.13 with Homebrew.'; brew install python@3.13; fi
        if ! command -v git >/dev/null 2>&1; then echo 'Installing Git with Homebrew.'; brew install git; fi
        if ! python_ok; then PATH="$(brew --prefix python@3.13)/libexec/bin:$PATH"; export PATH; fi
    elif command -v apt-get >/dev/null 2>&1; then
        echo 'Installing missing Python/Git requirements using apt. Your OS may ask for administrator authorization.'
        sudo apt-get update
        if ! python_ok; then sudo apt-get install -y python3 python3-venv; fi
        if ! command -v git >/dev/null 2>&1; then sudo apt-get install -y git; fi
    else
        echo 'Install Python 3.11 or newer and Git from their official websites or your OS package manager, then run this command again.' >&2
        exit 1
    fi
fi
python_ok || { echo 'Python 3.11 or newer is required. Install it and run this command again.' >&2; exit 1; }
for optional in node codex; do
    if command -v "$optional" >/dev/null 2>&1; then echo "$optional is available (optional)."
    else echo "$optional is not installed; chatting does not need it."; fi
done
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -f "$script_dir/tools/launch.py" ]; then install_dir=$script_dir
elif [ ! -e "$install_dir" ]; then
    echo "Downloading SnapLab into $install_dir."
    git clone -- "$repository" "$install_dir"
elif [ ! -f "$install_dir/tools/launch.py" ]; then
    echo 'The installation folder already contains other files. Choose another SNAPLAB_INSTALL_DIR.' >&2; exit 1
fi
if [ ! -d "$install_dir/.venv" ]; then
    echo "Creating a Python environment in $install_dir/.venv."
    python3 -m venv "$install_dir/.venv" || { echo 'Install the python3-venv package for this Python version and run this command again.' >&2; exit 1; }
fi
venv_python="$install_dir/.venv/bin/python"
if ! "$venv_python" -c 'import cryptography, keyring' 2>/dev/null; then
    echo "Installing cryptography and keyring in $install_dir/.venv. Keys use macOS Keychain or Linux Secret Service."
    "$venv_python" -m pip install cryptography keyring
fi
"$venv_python" "$install_dir/tools/launch.py" --data "$data_dir" --port "$port"
trap - 0
