"""Prepare the dedicated SnapLab WSL distribution without changing Windows settings."""
from __future__ import annotations

import hashlib
import argparse
import io
import json
import os
import platform
from pathlib import Path
import subprocess
import sys
import socket
import secrets
import shlex
import tarfile
import time
import urllib.request

DISTRO = "SnapLab-Worker"
BASE = "https://cdimage.ubuntu.com/ubuntu-base/releases/24.04/release/"
IMAGE = "ubuntu-base-24.04.5-base-amd64.tar.gz"
WSL_CONF = """[automount]
enabled=false
mountFsTab=false
[interop]
enabled=false
appendWindowsPath=false
[network]
generateResolvConf=true
[user]
default=root
"""

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def configure(root, *, packages=True):
    from snaplab.harnesses.wsl import MANAGED, version
    if packages:
        script = ("set -eu\nexport DEBIAN_FRONTEND=noninteractive\napt-get update\n"
                  "apt-get install -y --no-install-recommends python3 python-is-python3 bubblewrap libseccomp2 openssh-server ca-certificates curl nodejs npm iproute2 procps\n"
                  "id snaplab >/dev/null 2>&1 || useradd -m -s /bin/bash snaplab\npasswd -l snaplab\n"
                  "install -d -m 755 /opt/snaplab /run/sshd\n")
        result = wsl('-d', DISTRO, '-u', 'root', '--cd', '/', '--', 'sh', '-s', input=script.encode(), timeout=900)
        if result.returncode:
            raise RuntimeError('Linux package setup failed; SnapCode remains disabled')
    helpers = {Path(name).name: (ROOT / name).read_bytes() for name in MANAGED}
    helpers['remote_worker.py'] = (ROOT / 'snaplab/harnesses/remote_worker.py').read_bytes()
    helpers['setup_worker.sh'] = (ROOT / 'tools/setup_worker.sh').read_bytes()
    helpers['resolv.conf'] = b'nameserver 127.0.0.1\n'
    helpers['hosts'] = b'127.0.0.1 localhost\n::1 localhost\n'
    manifest = {name: hashlib.sha256(raw).hexdigest() for name, raw in helpers.items()}
    helpers['manifest.json'] = json.dumps(manifest).encode()
    bundle = io.BytesIO()
    with tarfile.open(fileobj=bundle, mode='w') as archive:
        for name, raw in helpers.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(raw), 0o644
            archive.addfile(info, io.BytesIO(raw))
    # Keep a root-only backup before replacing existing managed configuration/helpers.
    backup = wsl('-d', DISTRO, '-u', 'root', '--cd', '/', '--', 'python3', '-I', '-c',
                 "import pathlib,shutil,time; p=pathlib.Path('/opt/snaplab'); p.mkdir(exist_ok=True); "
                 "b=pathlib.Path('/root')/('snaplab-backup-'+str(time.time_ns())); b.mkdir(mode=448); "
                 "[shutil.copy2(f,b/f.name) for f in p.iterdir() if f.is_file() and not f.is_symlink()]")
    if backup.returncode:
        raise RuntimeError('Could not back up managed Linux files')
    result = wsl('-d', DISTRO, '-u', 'root', '--cd', '/', '--', 'tar', '-x', '-C', '/opt/snaplab', input=bundle.getvalue())
    if result.returncode:
        raise RuntimeError('Could not install the isolation helpers')
    if packages:
        script = """set -eu
export DEBIAN_FRONTEND=noninteractive
sh /opt/snaplab/setup_worker.sh snaplab --wsl
npm install --prefix /opt/snaplab/toolchain @openai/codex
if ! command -v google-chrome >/dev/null; then
  curl --fail --location --output /tmp/snaplab-chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
  apt-get install -y --no-install-recommends /tmp/snaplab-chrome.deb
  rm /tmp/snaplab-chrome.deb
fi
test -e /usr/local/bin/chromium || ln -s /opt/google/chrome/chrome /usr/local/bin/chromium
test -e /usr/local/bin/codex || ln -s /opt/snaplab/toolchain/node_modules/.bin/codex /usr/local/bin/codex
"""
        result = wsl('-d', DISTRO, '-u', 'root', '--cd', '/', '--', 'sh', '-s', input=script.encode(), timeout=1800)
        if result.returncode:
            raise RuntimeError('Linux desktop setup failed; SnapCode remains disabled')
    identity = root / 'identity'
    if not identity.exists():
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(identity)], check=True, capture_output=True)
    # Restrict this newly managed SSH key to its owner. No Windows-wide policy changes.
    from snaplab.harnesses.wsl import protect_key
    protect_key(identity)
    public = identity.with_suffix('.pub').read_text(encoding='ascii').strip()
    # Passwords and SSH agent forwarding are disabled. The key is only for this distro.
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    conf = f'''Port {port}
ListenAddress 127.0.0.1
HostKey /etc/ssh/ssh_host_ed25519_key
PidFile /run/snaplab-sshd.pid
AuthorizedKeysFile /opt/snaplab/authorized_keys
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
PermitRootLogin no
AllowUsers snaplab
AllowAgentForwarding no
X11Forwarding no
AllowTcpForwarding remote
GatewayPorts no
PermitTunnel no
UsePAM yes
PrintMotd no
'''
    for name, raw in [('sshd.conf', conf.encode()), ('authorized_keys', (public + '\n').encode())]:
        result = wsl('-d', DISTRO, '-u', 'root', '--cd', '/', '--', 'tee', '/opt/snaplab/' + name, input=raw)
        if result.returncode:
            raise RuntimeError('SSH configuration failed')
        manifest[name] = hashlib.sha256(raw).hexdigest()
    updated = wsl('-d', DISTRO, '-u', 'root', '--cd', '/', '--', 'tee', '/opt/snaplab/manifest.json',
                  input=json.dumps(manifest).encode())
    if updated.returncode:
        raise RuntimeError('Integrity manifest could not be installed')
    host = wsl('-d', DISTRO, '-u', 'root', '--cd', '/', '--', 'cat', '/etc/ssh/ssh_host_ed25519_key.pub')
    if host.returncode:
        raise RuntimeError('SSH host identity unavailable')
    known = root / 'known_hosts'
    if known.exists():
        known.replace(root / ('known_hosts.backup-' + str(time.time_ns())))
    known.write_text(f'[127.0.0.1]:{port} ' + host.stdout.decode('ascii').strip() + '\n', encoding='ascii')
    # Probe without a model or credentials, using the same Bubblewrap wrapper as jobs.
    probe = '''import sys,os,pathlib
sys.path.insert(0,"/opt/snaplab")
from remote_worker import isolated_command
from wsl_runtime import outer
home=pathlib.Path.home(); folder=home/"jobs/setup-probe"; folder.mkdir(parents=True,exist_ok=True)
config=home/"probe-config"; config.mkdir(exist_ok=True)
raise SystemExit(outer(isolated_command(["/bin/true"],folder,home,config),probe_only=True))
'''
    state = {'version': version(), 'isolation_ok': True, 'ssh_port': port}
    # Also exercise the root cgroup supervisor and the pinned SSH identity before declaring ready.
    from snaplab.harnesses.wsl import worker
    from snaplab.harnesses.remote import ssh_command
    from snaplab.harnesses.wsl import host_addresses
    from snaplab.harnesses.wsl_network import connect_public
    from snaplab.proxy import Translator
    token = secrets.token_urlsafe(32)
    windows_report = {}
    translator = Translator(port=0, api_cfg={'base_url': 'http://invalid', 'model': 'setup'},
                            slots=None, key_fn=lambda: None, token=token,
                            log_path=root / 'setup-proxy.log',
                            egress=lambda host, port: connect_public(host, port, host_addresses, reject_self=True))
    try:
        if not translator.start():
            raise RuntimeError('Could not start setup Internet broker')
        from check_wsl_windows_exec import check_windows_exec, windows_observer
        with windows_observer(windows_report) as marker, worker(_record=state, _locked=True) as cfg:
            cmd = ssh_command(cfg, translator.port, 39999)
            environment = {'SNAPLAB_RELAY_PORT': '39999', 'SNAPLAB_EGRESS_TOKEN': token,
                           'SNAPLAB_HOST_ADDRESSES': json.dumps(cfg['host_addresses'])}
            # The one-turn token travels only through SSH stdin, never argv or a file.
            cmd[-1] = 'python3 -I -c ' + shlex.quote('import sys; exec(sys.stdin.read())')
            code = 'import os\nos.environ.update(' + repr(environment) + ')\n' + probe
            result = subprocess.run(cmd, input=code.encode(), capture_output=True, timeout=120)
            (root / 'isolation-result.json').write_bytes(result.stdout)
            if result.returncode or json.loads(result.stdout).get('ok') is not True:
                raise RuntimeError('WSL isolation checks failed; SnapCode remains disabled')
            def run_linux(source):
                code = 'import os\nos.environ.update(' + repr(environment) + ')\n' + source
                return subprocess.run(cmd, input=code.encode(), capture_output=True, timeout=120)

            check_windows_exec(run_linux, windows_report, marker)
    except BaseException:
        (root / 'ready.json').unlink(missing_ok=True)
        raise
    finally:
        translator.stop()
        (root / 'windows-exec-result.json').write_text(json.dumps(windows_report, indent=2), encoding='utf-8')
    (root / 'ready.json').write_text(json.dumps(state), encoding='utf-8')


def wsl(*args, **kwargs):
    return subprocess.run(["wsl.exe", *args], capture_output=True, **kwargs)


def decode(raw):
    return raw.decode("utf-16-le" if b"\x00" in raw else "utf-8", "replace").strip()


def record_completion(root, start, resumed):
    result = {'ready': True, 'resumed': resumed, 'seconds': round(time.monotonic() - start, 2),
              'disk_bytes': sum(p.stat().st_size for p in root.rglob('*') if p.is_file())}
    (root / 'setup-completion.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--resume', action='store_true', help='Resume only the previously created SnapLab environment')
    parser.add_argument('--skip-packages', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if os.name != "nt":
        raise SystemExit("This setup requires Windows with WSL2 already installed.")
    if platform.machine().lower() not in ('amd64', 'x86_64'):
        raise SystemExit('This worker installer currently supports x64 Windows only. Chat remains available.')
    start = time.monotonic()
    from snaplab.harnesses.wsl import directory
    root = directory()
    root.mkdir(parents=True, exist_ok=True)
    listed = wsl("--list", "--quiet")
    if listed.returncode:
        raise SystemExit("WSL is unavailable. Install WSL2 using Microsoft's instructions, restart if requested, then run setup again. This program will not enable Windows features or request administrator access.")
    if DISTRO in decode(listed.stdout).splitlines():
        if not args.resume or not (root / 'setup-result.json').exists():
            raise SystemExit("SnapLab-Worker already exists. Nothing was overwritten. To update a SnapLab-created environment, run tools/setup_wsl.py --resume.")
        from snaplab.harnesses.wsl import exclusive
        with exclusive():
            (root / 'ready.json').unlink(missing_ok=True)
            configure(root, packages=not args.skip_packages)
        print('Dedicated WSL workspace passed isolation and startup checks.')
        record_completion(root, start, True)
        return
    archive = root / IMAGE
    sums = urllib.request.urlopen(BASE + "SHA256SUMS", timeout=60).read().decode("ascii")
    expected = next(line.split()[0] for line in sums.splitlines() if line.split()[-1].lstrip("*") == IMAGE)
    urllib.request.urlretrieve(BASE + IMAGE, archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
        raise SystemExit("Ubuntu image checksum mismatch; import refused.")
    prepared = root / "prepared.tar"
    with tarfile.open(archive, "r:gz") as src, tarfile.open(prepared, "w") as dst:
        for member in src:
            if member.name.lstrip("./") == "etc/wsl.conf":
                continue
            dst.addfile(member, src.extractfile(member) if member.isfile() else None)
        content = WSL_CONF.encode("utf-8")
        info = tarfile.TarInfo("etc/wsl.conf")
        info.size, info.mode = len(content), 0o644
        dst.addfile(info, io.BytesIO(content))
    target = root / "distribution"
    imported = wsl("--import", DISTRO, str(target), str(prepared), "--version", "2")
    if imported.returncode:
        raise SystemExit("WSL import failed: " + decode(imported.stderr + imported.stdout))
    # Start only our distribution. Do not call --shutdown or touch docker-desktop.
    check = wsl("-d", DISTRO, "-u", "root", "--cd", "/", "--", "/bin/sh", "-c", "id -u; uname -r; test ! -e /mnt/c/Windows")
    result = {"distribution": DISTRO, "seconds": round(time.monotonic() - start, 2),
              "download_bytes": archive.stat().st_size,
              "disk_bytes": sum(p.stat().st_size for p in root.rglob('*') if p.is_file()),
              "boot_returncode": check.returncode, "boot_output": decode(check.stdout + check.stderr)}
    (root / "setup-result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    if check.returncode:
        raise SystemExit("Dedicated WSL environment could not boot. SnapCode remains disabled.")
    from snaplab.harnesses.wsl import exclusive
    with exclusive():
        configure(root)
    record_completion(root, start, False)


if __name__ == "__main__":
    main()
