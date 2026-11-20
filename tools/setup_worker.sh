#!/bin/sh
# Run with sudo on Debian/Ubuntu. Uses an existing unprivileged worker account.
set -eu
worker=${1:-snaplab}
case "$worker" in *[!a-z0-9_-]*|'') echo 'Invalid worker user' >&2; exit 1;; esac
uid=$(id -u "$worker")
[ "$uid" -ne 0 ]
# Only the Pi install can also be run by the existing worker account.
if [ "${2:-}" = "--pi-only" ]; then
  [ "$(id -un)" = "$worker" ]
  export PATH="$HOME/.local/bin:$PATH"
  # Existing workers can be upgraded without administrator access.
  if ! command -v python >/dev/null 2>&1; then
    mkdir -p "$HOME/.local/bin"
    ln -s /usr/bin/python3 "$HOME/.local/bin/python"
  fi
  if ! node -e 'const [a,b]=process.versions.node.split(".").map(Number);process.exit(a>22||a===22&&b>=19?0:1)'; then
    npm install --global --prefix "$HOME/.local" node@22.22.1
  fi
  npm install --global --prefix "$HOME/.local" @earendil-works/pi-coding-agent@1.5.0
  "$HOME/.local/bin/pi" --version
  exit 0
fi
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  bubblewrap xvfb xauth xdotool openbox scrot xclip xterm mousepad \
  libreoffice-writer python3 python-is-python3 python3-tk python3-pil fonts-noto-cjk \
  tesseract-ocr tesseract-ocr-eng tesseract-ocr-jpn dbus-x11
# Dedicated WSL jobs are limited by the root-owned SSH supervisor's cgroup.
runuser -u "$worker" -- sh "$0" "$worker" --pi-only
if [ "${2:-}" = "--wsl" ]; then
  echo 'Worker desktop dependencies installed; the WSL supervisor supplies resource limits.'
  exit 0
fi
# The worker's jobs together cannot exhaust the host: the account may use all memory but 1.5 GiB (at least 3 GiB).
# Individual jobs get 3 GiB and 1024 tasks; a desktop job with Chrome and DevTools uses about 600 MiB and 240 tasks.
# No MemoryHigh: throttling below the limit raises memory pressure, and systemd-oomd then kills whole jobs.
total_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
max_mb=$((total_mb - 1536)); [ "$max_mb" -ge 3072 ] || max_mb=3072
dir="/etc/systemd/system/user-${uid}.slice.d"
mkdir -p "$dir"
conf="$dir/50-snaplab.conf"
if [ -f "$conf" ]; then cp -p "$conf" "$conf.bak-$(date +%Y%m%d%H%M%S)"; fi
printf '[Slice]\nMemoryHigh=infinity\nMemoryMax=%sM\nMemorySwapMax=0\nTasksMax=8192\n' "$max_mb" > "$conf"
systemctl daemon-reload
loginctl enable-linger "$worker"
systemctl start "user@${uid}.service"
systemctl set-property --runtime "user-${uid}.slice" MemoryHigh=infinity MemoryMax="${max_mb}M" MemorySwapMax=0 TasksMax=8192
echo 'Worker desktop dependencies and resource limits installed. Install Codex and Chromium separately.'
