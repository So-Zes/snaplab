"""Adversarial, model-free probes. Run INSIDE the actual WSL job sandbox; any failure blocks work."""
from __future__ import annotations

import errno
import ctypes
import json
import os
from pathlib import Path
import socket
import subprocess
import re
import signal
import tempfile
import time
import urllib.error
import urllib.request


def probe_executable(argv, timeout=1):
    """No shell fallback; a timeout or an unexplained exit is NOT a denial."""
    start = time.monotonic()
    try:
        child = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 start_new_session=True)
    except OSError as error:
        return {'denied': error.errno in (errno.ENOEXEC, errno.ENOENT, errno.EPERM, errno.EACCES),
                'errno': error.errno, 'seconds': round(time.monotonic() - start, 4)}
    try:
        stdout, stderr = child.communicate(timeout=timeout)
        # With binfmt's F flag /init can execute even though its path is hidden.
        # Accept only its specific immediate EPERM/EACCES socket failure, never
        # the old accept4 timeout (which can even return exit code zero).
        vsock_denied = bool(re.fullmatch(
            rb'<3>WSL \([^\r\n]+\) ERROR: UtilBindVsockAnyPort:[0-9]+: socket failed (1|13)\r?\n', stderr))
        # SSH-started processes can stop earlier while traversing the private
        # PID namespace: PID 1 is bwrap, not the host WSL init/session leader.
        parent_denied = bool(re.fullmatch(
            rb'<3>WSL \([^\r\n]+\) ERROR: UtilGetPpid:[0-9]+: Failed to parse: /proc/1/stat, content: 1 \(bwrap\) [^\r\n]+\r?\n', stderr))
        denied = child.returncode != 0 and not stdout and (vsock_denied or parent_denied)
        return {'denied': denied, 'returncode': child.returncode,
                'vsock_permission_denied': vsock_denied, 'private_parent_denied': parent_denied,
                'seconds': round(time.monotonic() - start, 4)}
    except subprocess.TimeoutExpired:
        return {'denied': False, 'timeout': True, 'seconds': round(time.monotonic() - start, 4)}
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.communicate()


def probe_mz(directory):
    with tempfile.TemporaryDirectory(prefix='.snaplab-mz-', dir=directory) as tmp:
        path = Path(tmp) / 'probe.exe'
        path.write_bytes(b'MZ')
        path.chmod(0o700)
        return probe_executable([str(path)])


def run():
    checks = []

    def record(name, ok):
        checks.append({'name': name, 'ok': bool(ok)})

    record('unprivileged', os.getuid() != 0 and os.geteuid() != 0 and 0 not in os.getgroups())
    status = Path('/proc/self/status').read_text()
    record('no_new_privileges', 'NoNewPrivs:\t1' in status)
    record('seccomp_filter', 'Seccomp:\t2' in status)
    for path in ('/mnt/c', '/mnt/wsl', '/mnt/wslg', '/init', '/run/WSL', '/dev/vsock',
                 '/proc/sys/fs/binfmt_misc/WSLInterop', '/var/run/docker.sock',
                 '/home/root', '/root', '/run/user/1000/bus'):
        record('hidden:' + path, not os.path.exists(path))
    exposed = []
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        before, after = line.split(' - ', 1)
        fields = before.split()
        if after.split()[0] not in ('9p', 'drvfs'):
            continue
        # mountinfo includes covered mounts. Require the dangerous mount to be unreachable.
        try:
            dev = os.stat(fields[4]).st_dev
            if f'{os.major(dev)}:{os.minor(dev)}' == fields[2]:
                exposed.append(fields[4])
        except FileNotFoundError:
            pass
    record('no_host_mounts', not exposed)
    result = subprocess.run(['/bin/umount', '/usr/lib/wsl'], capture_output=True, timeout=3)
    record('cannot_uncover_wsl_drivers', result.returncode != 0 and not Path('/usr/lib/wsl/drivers').exists())
    for executable in ('cmd.exe', 'powershell.exe', '/init'):
        try:
            result = subprocess.run([executable, '/c', 'exit'], capture_output=True, timeout=3)
            record('exec:' + executable, False)
        except (OSError, subprocess.TimeoutExpired):
            record('exec:' + executable, True)
    for name, directory in (('workspace', Path.cwd()), ('tmp', Path('/tmp'))):
        result = probe_mz(directory)
        checks.append({'name': 'direct_mz:' + name, 'ok': result['denied'], **result})
    Path('/tmp/mount-probe').mkdir(exist_ok=True)
    for filesystem, source in (('drvfs', 'C:'), ('9p', 'drvfs'), ('proc', 'proc')):
        result = subprocess.run(['/bin/mount', '-t', filesystem, source, '/tmp/mount-probe'], capture_output=True, timeout=3)
        record('mount:' + filesystem, result.returncode != 0)
    try:
        sock = socket.socket(40, socket.SOCK_STREAM)  # AF_VSOCK must be forbidden, not merely disconnected.
        sock.close()
        record('vsock_denied', False)
    except OSError as error:
        record('vsock_denied', error.errno in (errno.EPERM, errno.EACCES))
    libc = ctypes.CDLL(None, use_errno=True)
    # The supported worker is x86_64. Exercise int truncation via raw syscalls.
    for number, name in ((41, 'socket'), (53, 'socketpair')):
        pair = (ctypes.c_int * 2)(-1, -1)
        ctypes.set_errno(0)
        value = libc.syscall(ctypes.c_long(number), ctypes.c_ulonglong((1 << 32) | 40),
                             ctypes.c_int(1), ctypes.c_int(0), ctypes.byref(pair))
        record('vsock_high_bits_denied:' + name, value == -1 and ctypes.get_errno() in (errno.EPERM, errno.EACCES))
        if value >= 0:
            for fd in ([value] if name == 'socket' else pair):
                os.close(fd)
    record('only_loopback', set(os.listdir('/sys/class/net')) == {'lo'} if Path('/sys/class/net').exists()
           else len(Path('/proc/net/dev').read_text().splitlines()) == 3)
    record('no_routes', len(Path('/proc/net/route').read_text().splitlines()) == 1)
    record('private_pid_namespace', os.readlink('/proc/self/ns/pid') != os.environ['SNAPLAB_OUTSIDE_PID_NS'])
    canary = os.environ['SNAPLAB_OUTSIDE_CANARY']
    for mode in ('rb', 'wb'):
        try:
            with open(canary, mode):
                record('outside_file:' + mode, False)
        except OSError:
            record('outside_file:' + mode, True)
    try:
        os.kill(int(os.environ['SNAPLAB_OUTSIDE_PID']), 0)
        record('outside_process', False)
    except ProcessLookupError:
        record('outside_process', True)
    nested = subprocess.run(['/usr/bin/unshare', '-Ur', '/bin/true'], capture_output=True, timeout=3)
    record('nested_user_namespace_denied', nested.returncode != 0)
    record('resource_policy_hidden', not Path('/sys/fs/cgroup').exists())
    record('network_policy_readonly', not os.access('/opt/snaplab/wsl_network.py', os.W_OK))
    if os.environ.get('SNAPLAB_MODEL_RELAY_REQUIRED') == '1':
        direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request('http://127.0.0.1:' + os.environ['SNAPLAB_RELAY_PORT'] + '/v1/responses', data=b'{}')
        try:
            direct.open(request, timeout=5).close()
            record('model_relay_requires_token', False)
        except urllib.error.HTTPError as error:
            record('model_relay_requires_token', error.code == 401)
        except OSError:
            record('model_relay_requires_token', False)
    # No route must block even a newly appearing LAN service, not just currently closed ports.
    targets = ['172.16.0.1', '172.31.255.254', '192.168.0.1', '192.168.1.1', '10.0.0.1',
               '100.64.0.1', '100.127.255.254', '169.254.169.254']
    targets += json.loads(os.environ.get('SNAPLAB_HOST_ADDRESSES', '[]'))
    for host in sorted(set(targets)):
        for port in (445, 8445, 18445, 8444, 18440):
            try:
                with socket.create_connection((host, port), timeout=.15):
                    record('direct:' + host + ':' + str(port), False)
            except OSError:
                record('direct:' + host + ':' + str(port), True)
    # Override bypass rules so even localhost is actually submitted to the broker.
    os.environ['NO_PROXY'] = os.environ['no_proxy'] = ''
    proxy = urllib.request.build_opener(urllib.request.ProxyHandler({'http': 'http://127.0.0.1:18080'}))
    for host in ['127.0.0.1', '10.0.0.1', '172.16.0.1', '192.168.1.1', '100.64.0.1',
                 '169.254.169.254', '[::1]', 'example.ts.net', 'agent-pc', 'host.docker.internal']:
        try:
            proxy.open('http://' + host + '/', timeout=3).close()
            record('proxy:' + host, False)
        except urllib.error.HTTPError as error:
            record('proxy:' + host, error.code == 403)
        except OSError:
            record('proxy:' + host, False)  # Missing/broken broker is not evidence of filtering.
    # The public route must work. Internet outages fail closed too.
    try:
        with proxy.open('http://example.com/', timeout=15) as response:
            record('public_internet', response.status == 200)
    except OSError:
        record('public_internet', False)
    record('other_jobs_hidden', not Path('/home/snaplab/jobs').exists() or
           len(list(Path('/home/snaplab/jobs').iterdir())) == 1)
    record('host_processes_hidden', all('sshd' not in p.read_text(errors='replace')
           for p in Path('/proc').glob('[0-9]*/comm') if p.exists()))
    result = {'ok': all(item['ok'] for item in checks), 'checks': checks}
    print(json.dumps(result), flush=True)
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(run())
