#!/bin/sh
# Prepare the SnapCode workspace on this Linux PC (Debian or Ubuntu with systemd). install.sh runs it with sudo:
#
#     sudo sh tools/setup_linux_worker.sh <SnapLab's public key file>
#
# It creates a separate, locked "snaplab" account for SnapCode jobs, installs the desktop tools, Pi, Codex and Chrome
# for it (tools/setup_worker.sh), and lets SnapLab reach it over SSH from this PC only (127.0.0.1, SnapLab's own key).
# Jobs run inside Bubblewrap in that account, the same isolation as on the WSL worker and on a separate Linux PC.
set -eu
[ "$(id -u)" -eq 0 ] || { echo 'Run this with sudo.' >&2; exit 1; }
key_file=${1:-}
[ -n "$key_file" ] && [ -f "$key_file" ] || { echo 'Give the public key file SnapLab made (install.sh does this).' >&2; exit 1; }
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
command -v apt-get >/dev/null 2>&1 || { echo 'This script supports Debian and Ubuntu (apt). See docs/linux.md for other distributions.' >&2; exit 1; }
[ -d /run/systemd/system ] || { echo 'SnapCode needs systemd (it limits the memory jobs may use).' >&2; exit 1; }
export DEBIAN_FRONTEND=noninteractive
echo 'Installing SSH, Bubblewrap, Python and Node.js for the SnapCode account.'
apt-get update
apt-get install -y --no-install-recommends openssh-server openssh-client bubblewrap python3 python-is-python3 \
  nodejs npm curl ca-certificates git iproute2 procps
id snaplab >/dev/null 2>&1 || useradd -m -s /bin/bash snaplab
passwd -l snaplab >/dev/null
home=$(getent passwd snaplab | cut -d: -f6)

# Ubuntu 24.04 can forbid unprivileged user namespaces, which Bubblewrap needs. Allow them for bwrap only.
if ! runuser -u snaplab -- bwrap --ro-bind / / --unshare-user --unshare-pid --dev /dev --proc /proc true 2>/dev/null; then
  if [ -d /etc/apparmor.d ] && command -v apparmor_parser >/dev/null 2>&1; then
    cat > /etc/apparmor.d/snaplab-bwrap <<'EOF'
abi <abi/4.0>,
include <tunables/global>
profile snaplab-bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,
}
EOF
    apparmor_parser -r /etc/apparmor.d/snaplab-bwrap
  fi
  runuser -u snaplab -- bwrap --ro-bind / / --unshare-user --unshare-pid --dev /dev --proc /proc true \
    || { echo 'Bubblewrap cannot create a sandbox on this system, so SnapCode stays off. See docs/linux.md.' >&2; exit 1; }
fi

echo 'Installing the desktop tools, Pi and resource limits for the SnapCode account.'
# setup_worker.sh runs part of itself as the snaplab account, which cannot read the installing user's home folder.
install -d -m 755 /opt/snaplab
install -m 644 "$here/setup_worker.sh" /opt/snaplab/setup_worker.sh
sh /opt/snaplab/setup_worker.sh snaplab
echo 'Installing Codex for the SnapCode account.'
runuser -u snaplab -- sh -c 'export PATH="$HOME/.local/bin:$PATH"; npm install --global --prefix "$HOME/.local" @openai/codex'
if [ "$(dpkg --print-architecture)" = amd64 ] && ! command -v google-chrome >/dev/null 2>&1; then
  echo 'Installing Google Chrome for computer use.'
  curl --fail --location --output /tmp/snaplab-chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
  apt-get install -y --no-install-recommends /tmp/snaplab-chrome.deb
  rm -f /tmp/snaplab-chrome.deb
fi
[ -e /usr/local/bin/chromium ] || [ ! -e /opt/google/chrome/chrome ] || ln -s /opt/google/chrome/chrome /usr/local/bin/chromium

# SSH: this key, from this PC only, with remote port forwarding (SnapLab's translator) and nothing else.
install -d -m 700 -o snaplab -g snaplab "$home/.ssh"
# SNAPLAB_SSH_FROM: a Mac's Lima VM sees the connection arrive through Lima's forward, so its script passes '*'.
from=${SNAPLAB_SSH_FROM:-127.0.0.1,::1}
if [ "$from" = '*' ]; then options='restrict,port-forwarding'; else options="from=\"$from\",restrict,port-forwarding"; fi
printf '%s %s\n' "$options" "$(head -n 1 "$key_file")" > "$home/.ssh/authorized_keys"
chown snaplab:snaplab "$home/.ssh/authorized_keys"
chmod 600 "$home/.ssh/authorized_keys"
systemctl enable --now ssh 2>/dev/null || systemctl enable --now sshd
echo 'The SnapCode workspace is ready.'
