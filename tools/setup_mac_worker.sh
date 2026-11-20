#!/bin/sh
# Prepare the SnapCode workspace on a Mac: a small Ubuntu virtual machine made with Lima (https://lima-vm.io), with the
# same locked "snaplab" account as on Linux (tools/setup_linux_worker.sh). install.sh runs this on macOS when asked.
#
#     sh tools/setup_mac_worker.sh
#
# The VM uses Apple's Virtualization framework (macOS 13 or later), 4 CPUs, 6 GiB of memory and up to 40 GiB of disk.
# SnapLab reaches the snaplab account through the SSH port Lima forwards to 127.0.0.1, with its own key and a pinned
# host key, exactly like the Linux worker.
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
wdir="$HOME/.snaplab/mac-worker"
vm=snaplab
mkdir -p "$wdir"; chmod 700 "$wdir"
command -v brew >/dev/null 2>&1 || { echo 'Install Homebrew from https://brew.sh first.' >&2; exit 1; }
command -v limactl >/dev/null 2>&1 || { echo 'Installing Lima with Homebrew.'; brew install lima; }
if ! limactl list --format '{{.Name}}' 2>/dev/null | grep -qx "$vm"; then
    echo 'Creating the Ubuntu VM for SnapCode (downloads about 600 MB).'
    limactl create --name "$vm" --tty=false --vm-type=vz --cpus=4 --memory=6 --disk=40 \
        --set '.mounts=[]' template://ubuntu-lts
fi
limactl start "$vm" --tty=false
[ -f "$wdir/identity" ] || ssh-keygen -q -t ed25519 -N '' -C snaplab-worker -f "$wdir/identity"
# Copy the two setup scripts and the key into the VM (it has no access to the Mac's files) and run them there.
tmp=$(limactl shell "$vm" -- mktemp -d)
limactl copy "$here/setup_linux_worker.sh" "$here/setup_worker.sh" "$wdir/identity.pub" "$vm:$tmp/"
# Inside the VM the SSH connection from the Mac arrives through Lima's forward, not from 127.0.0.1.
limactl shell "$vm" -- sudo env SNAPLAB_SSH_FROM='*' sh "$tmp/setup_linux_worker.sh" "$tmp/identity.pub"
port=$(limactl list "$vm" --format '{{.SSHLocalPort}}')
ssh-keyscan -T 10 -t ed25519 -p "$port" 127.0.0.1 2>/dev/null > "$wdir/known_hosts"
ssh -F /dev/null -i "$wdir/identity" -o IdentitiesOnly=yes -o UserKnownHostsFile="$wdir/known_hosts" \
    -o GlobalKnownHostsFile=/dev/null -o StrictHostKeyChecking=yes -o BatchMode=yes -p "$port" snaplab@127.0.0.1 true
printf '{"ssh_port": %s, "vm": "%s"}\n' "$port" "$vm" > "$wdir/ready.json"
echo "The SnapCode workspace is ready (Lima VM \"$vm\", SSH on 127.0.0.1:$port)."
