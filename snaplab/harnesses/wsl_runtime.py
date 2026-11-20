"""Trusted WSL job wrapper; installed root-owned outside every job mount namespace."""
from __future__ import annotations

import ctypes
import errno
import json
import os
from pathlib import Path
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading

# -I ignores writable working directories; only this root-owned directory is added.
sys.path.insert(0, '/opt/snaplab')
from wsl_network import Broker, pump, public_addresses


def connect_via_windows(host, port):
    # Both sides validate. Only Windows opens public connections, with a fresh
    # NIC inventory and a connected-socket self check before passing any bytes.
    if port not in (80, 443):
        raise ValueError('unsupported port')
    answers = public_addresses(host, port, json.loads(os.environ['SNAPLAB_HOST_ADDRESSES']))
    ip = answers[0][4][0]
    sock = socket.create_connection(('127.0.0.1', int(os.environ['SNAPLAB_RELAY_PORT'])), timeout=30)
    try:
        token = os.environ['SNAPLAB_EGRESS_TOKEN']
        sock.sendall(f'CONNECT {ip}:{port} HTTP/1.1\r\nHost: {ip}:{port}\r\nProxy-Authorization: Bearer {token}\r\n\r\n'.encode('ascii'))
        header = bytearray()
        while not header.endswith(b'\r\n\r\n'):
            part = sock.recv(1)
            if not part or len(header) > 32768:
                raise ValueError('invalid tunnel response')
            header.extend(part)
        if bytes(header).split(b'\r\n', 1)[0].split(b' ')[1] != b'200':
            raise ValueError('Windows refused destination')
        return sock
    except BaseException:
        sock.close()
        raise


def filter_fd():
    lib = ctypes.CDLL('libseccomp.so.2', use_errno=True)
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    lib.seccomp_export_bpf.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    ctx = lib.seccomp_init(0x7fff0000)  # ALLOW, with explicit namespace-escape denials.
    if not ctx:
        raise RuntimeError('seccomp unavailable')
    fd = os.memfd_create('snaplab-seccomp')
    deny = 0x50000 | errno.EPERM
    try:
        for name in ('mount', 'umount2', 'fsopen', 'fsmount', 'fspick', 'move_mount', 'open_tree',
                     'mount_setattr', 'open_by_handle_at', 'bpf', 'ptrace', 'process_vm_readv',
                     'process_vm_writev', 'pidfd_getfd', 'keyctl', 'add_key', 'request_key',
                     'kexec_load', 'kexec_file_load', 'init_module', 'finit_module', 'delete_module',
                     'userfaultfd', 'setns'):
            number = lib.seccomp_syscall_resolve_name(name.encode())
            if number >= 0 and lib.seccomp_rule_add(ctx, deny, number, 0) != 0:
                raise RuntimeError('seccomp rule failed')
        class Comparison(ctypes.Structure):
            _fields_ = [('arg', ctypes.c_uint), ('op', ctypes.c_uint),
                        ('a', ctypes.c_uint64), ('b', ctypes.c_uint64)]
        # Only UNIX, IPv4, IPv6 and NETLINK. This also denies VSOCK and AF_PACKET.
        for syscall in (b'socket', b'socketpair'):
            number = lib.seccomp_syscall_resolve_name(syscall)
            for domain in range(0, 256):
                if domain not in (1, 2, 10, 16):
                    # socket's domain is an int: the kernel ignores the upper
                    # 32 bits even when a raw syscall supplies a 64-bit value.
                    if lib.seccomp_rule_add(ctx, deny, number, 1, Comparison(0, 7, 0xffffffff, domain)) != 0:
                        raise RuntimeError('socket filter failed')
        if lib.seccomp_export_bpf(ctx, fd) != 0:
            raise RuntimeError('seccomp export failed')
        os.lseek(fd, 0, os.SEEK_SET)
        return fd
    except BaseException:
        os.close(fd)
        raise
    finally:
        lib.seccomp_release(ctx)


class Relay(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            with socket.create_connection(('127.0.0.1', self.server.relay_port), timeout=20) as upstream:
                pump(self.request, upstream)
        except OSError:
            pass


class Inner(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            with socket.socket(socket.AF_UNIX) as upstream:
                upstream.connect(self.server.unix_path)
                pump(self.request, upstream)
        except OSError:
            pass


def inner(command):
    servers = []
    for port, path in ((18080, '/run/snaplab-egress/internet'),
                       (int(os.environ['SNAPLAB_RELAY_PORT']), '/run/snaplab-egress/model')):
        server = socketserver.ThreadingTCPServer(('127.0.0.1', port), Inner)
        server.daemon_threads = True
        server.unix_path = path
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
    env = dict(os.environ)
    for key in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY'):
        env[key] = 'http://127.0.0.1:18080'
    env['NO_PROXY'] = env['no_proxy'] = '127.0.0.1,localhost'
    try:
        return subprocess.call(command, env=env)
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def outer(command, *, probe_only=False):
    if os.getuid() == 0 or command[0] != '/usr/bin/bwrap':
        raise RuntimeError('unprivileged Bubblewrap is required')
    split = command.index('--')
    base, target = command[:split], command[split + 1:]
    for name in ('resolv.conf', 'hosts'):
        for index in range(len(base) - 2):
            if base[index:index + 3] == ['--ro-bind', '/etc/' + name, '/etc/' + name]:
                base[index + 1] = '/opt/snaplab/' + name
    fd = filter_fd()
    with tempfile.TemporaryDirectory(prefix='snaplab-egress-') as directory:
        canary = Path(directory) / 'outside-job-canary'
        canary.write_bytes(b'SnapLab synthetic isolation canary\n')
        # Lives until the finally below. A fixed 120 s made a probe that took longer (a busy PC) fail the job.
        outside_process = subprocess.Popen(['/bin/sleep', 'infinity'], stdin=subprocess.DEVNULL,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.environ['SNAPLAB_OUTSIDE_CANARY'] = str(canary)
        os.environ['SNAPLAB_OUTSIDE_PID'] = str(outside_process.pid)
        os.environ['SNAPLAB_OUTSIDE_PID_NS'] = os.readlink('/proc/self/ns/pid')
        host_addresses = set(json.loads(os.environ.get('SNAPLAB_HOST_ADDRESSES', '[]')))
        for line in Path('/proc/net/route').read_text().splitlines()[1:]:
            fields = line.split()
            if fields[1] == '00000000':
                host_addresses.add(socket.inet_ntoa(bytes.fromhex(fields[2])[::-1]))
        os.environ['SNAPLAB_HOST_ADDRESSES'] = json.dumps(sorted(host_addresses))
        sockets = Path(directory) / 'sockets'
        sockets.mkdir()
        servers = [Broker(sockets / 'internet'), Broker(sockets / 'model', Relay)]
        servers[0].blocked = host_addresses
        servers[0].connect = connect_via_windows
        servers[1].relay_port = int(os.environ['SNAPLAB_RELAY_PORT'])
        for server in servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
        strict = base + ['--unshare-net', '--disable-userns', '--seccomp', str(fd),
                         '--tmpfs', '/usr/lib/wsl', '--remount-ro', '/usr/lib/wsl',
                         '--ro-bind', '/opt/google/chrome', '/opt/google/chrome',
                         '--ro-bind', str(sockets), '/run/snaplab-egress',
                         '--ro-bind', '/opt/snaplab', '/opt/snaplab']
        try:
            # This runs before Codex, for every job and test command, in the same restrictions.
            probe = subprocess.run(strict + ['--', '/usr/bin/python3', '-I', '/opt/snaplab/wsl_runtime.py',
                                              '--inner', '/usr/bin/python3', '-I', '/opt/snaplab/check_wsl_isolation.py'],
                                   pass_fds=(fd,), capture_output=True)   # no time limit: a busy PC can be slow
            if probe.returncode:
                # Only our probe's bounded structured result, no arbitrary stderr or secrets.
                try:
                    result = json.loads(probe.stdout)
                    print(json.dumps({'kind': 'isolation_failed', 'checks': result['checks']}), flush=True)
                except (ValueError, KeyError):
                    print('{"kind":"isolation_failed"}', flush=True)
                return 125
            if canary.read_bytes() != b'SnapLab synthetic isolation canary\n' or outside_process.poll() is not None:
                return 125
            if probe_only:
                print(probe.stdout.decode('utf-8').strip(), flush=True)
                return 0
            os.lseek(fd, 0, os.SEEK_SET)
            job_env = dict(os.environ)
            for name in ('SNAPLAB_HOST_ADDRESSES', 'SNAPLAB_OUTSIDE_CANARY', 'SNAPLAB_OUTSIDE_PID',
                         'SNAPLAB_OUTSIDE_PID_NS', 'SNAPLAB_MODEL_RELAY_REQUIRED', 'SNAPLAB_EGRESS_TOKEN'):
                job_env.pop(name, None)
            return subprocess.call(strict + ['--', '/usr/bin/python3', '-I', '/opt/snaplab/wsl_runtime.py',
                                              '--inner', *target], pass_fds=(fd,), env=job_env)
        finally:
            outside_process.terminate()
            outside_process.wait(timeout=5)
            os.close(fd)
            for server in servers:
                server.shutdown()
                server.server_close()


if __name__ == '__main__':
    try:
        raise SystemExit(inner(sys.argv[2:]) if sys.argv[1] == '--inner' else outer(sys.argv[1:]))
    except Exception:
        print('{"kind":"isolation_failed"}', flush=True)
        raise SystemExit(125)
