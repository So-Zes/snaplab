"""Root-only SSH lifecycle and hard resource limits for the dedicated WSL distro."""
import os
import configparser
import grp
import pwd
from pathlib import Path
import subprocess
import sys

if os.getuid() != 0:
    raise SystemExit(1)

conf = configparser.ConfigParser()
conf.read('/etc/wsl.conf')
for section, name in [('automount', 'enabled'), ('automount', 'mountFsTab'),
                      ('interop', 'enabled'), ('interop', 'appendWindowsPath')]:
    if conf.getboolean(section, name, fallback=True):
        raise SystemExit('WSL Windows integration must be disabled')
user = pwd.getpwnam('snaplab')
if user.pw_uid == 0 or set(os.getgrouplist('snaplab', user.pw_gid)) != {user.pw_gid}:
    raise SystemExit('worker privileges changed')

# A separate cgroup limits this sshd and all job descendants; no global WSL limits change.
root = Path('/sys/fs/cgroup')
if not (root / 'cgroup.controllers').exists():
    raise SystemExit('cgroup v2 is required; refusing to start jobs')
group = root / 'snaplab-worker'
group.mkdir(exist_ok=True)
available = (root / 'cgroup.controllers').read_text().split()
if not {'memory', 'pids', 'cpu'} <= set(available):
    raise SystemExit('required cgroup controllers unavailable')
(root / 'cgroup.subtree_control').write_text('+memory +pids +cpu')
# The worker as a whole has no cap (2026-11-16): it was 4 GiB, 1024 tasks and two CPU cores for all jobs together,
# which serialized parallel SnapCode sessions. Each job still waits for free memory and runs in its own scope.
for name, value in {'memory.max': 'max', 'memory.swap.max': '0', 'pids.max': 'max', 'cpu.max': 'max'}.items():
    (group / name).write_text(value)
(group / 'cgroup.procs').write_text(str(os.getpid()))
Path('/run/sshd').mkdir(exist_ok=True)
os.execv('/usr/sbin/sshd', ['/usr/sbin/sshd', '-D', '-e', '-f', '/opt/snaplab/sshd.conf'])
