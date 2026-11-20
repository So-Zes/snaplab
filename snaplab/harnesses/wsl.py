"""On-demand lifecycle for the dedicated, verified local WSL worker."""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
import ctypes
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time

from .base import TurnResult

DISTRO = 'SnapLab-Worker'
ROOT = Path(__file__).resolve().parents[2]
MANAGED = ('snaplab/harnesses/wsl_runtime.py', 'snaplab/harnesses/wsl_network.py',
           'tools/check_wsl_isolation.py', 'tools/wsl_sshd.py', 'tools/setup_worker.sh')


# Small records and keys that SnapLab itself reads; the distribution's disk stays where WSL registered it.
STATE_FILES = ('ready.json', 'identity', 'identity.pub', 'known_hosts', 'active.lock', 'isolation-result.json',
               'setup-result.json', 'setup-completion.json', 'windows-exec-result.json', 'last-run-metrics.json')
_adopted = False


def directory():
    """The WSL worker's keys and records, outside AppData (2026-11-16). A program installed as an app package, such as
    the Claude desktop app from which setup is often run, gets its own private copy of AppData: what setup wrote there
    was invisible to the SnapLab logon task, which then reported wsl_not_ready although setup had passed."""
    root = Path.home() / '.snaplab' / 'wsl'
    _adopt_legacy(root)
    return root


def legacy_directory():
    base = os.environ.get('LOCALAPPDATA')
    return Path(base) / 'SnapLab' / 'wsl' if base else None


def _adopt_legacy(root):
    """Once per process: copy the records of a setup made under the old AppData location, so it keeps working."""
    global _adopted
    if _adopted:
        return
    _adopted = True
    old = legacy_directory()
    if not old or not old.is_dir() or (root / 'setup-result.json').exists():
        return
    import shutil
    root.mkdir(parents=True, exist_ok=True)
    for name in STATE_FILES:
        if (old / name).is_file() and not (root / name).exists():
            shutil.copy2(old / name, root / name)
            if name == 'identity':
                protect_key(root / name)     # a copy takes the folder's access rules; OpenSSH refuses a shared key


def protect_key(identity):
    """Leave the SSH key readable by its owner only, as OpenSSH requires. No Windows-wide policy changes."""
    acl = ("$sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User; "
           "$acl=[System.IO.File]::GetAccessControl($env:SNAPLAB_IDENTITY_FILE,'Access'); "
           "$acl.SetAccessRuleProtection($true,$false); "
           "foreach($rule in @($acl.Access)) { [void]$acl.RemoveAccessRuleSpecific($rule) }; "
           "$acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($sid,'FullControl','Allow'))); "
           "[System.IO.File]::SetAccessControl($env:SNAPLAB_IDENTITY_FILE,$acl)")
    subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', acl],
                   env=dict(os.environ, SNAPLAB_IDENTITY_FILE=str(identity)), check=True, capture_output=True)


def version():
    digest = hashlib.sha256()
    for name in MANAGED:
        digest.update((ROOT / name).read_bytes())
    return digest.hexdigest()


def ready():
    if os.name != 'nt':
        return False
    try:
        record = json.loads((directory() / 'ready.json').read_text(encoding='utf-8'))
        return record.get('version') == version() and record.get('isolation_ok') is True
    except (OSError, ValueError):
        return False


def available(app):
    if not ready():
        return False, 'wsl_not_ready'
    if not app.models.has_key(None, 'codex'):
        return False, 'API key not set'
    return True, ''


def command(*args):
    return ['wsl.exe', '-d', DISTRO, '-u', 'root', '--cd', '/', '--', *args]


class _AddressRow(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in ('address', 'index', 'mask', 'broadcast', 'reassembly')] + [
        ('unused', ctypes.c_uint16), ('kind', ctypes.c_uint16)]


class _AddressTable(ctypes.Structure):
    _fields_ = [('count', ctypes.c_uint32), ('rows', _AddressRow * 1)]


def _interface_table():
    # IP Helper is a read-only API. No PowerShell child process for each connection.
    query = ctypes.WinDLL('iphlpapi.dll').GetIpAddrTable
    query.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_int]
    query.restype = ctypes.c_uint32
    size = ctypes.c_uint32()
    if query(None, ctypes.byref(size), 0) != 122 or not 4 <= size.value <= 1048576:
        raise RuntimeError('Windows interface discovery failed')
    buffer = ctypes.create_string_buffer(size.value)
    if query(buffer, ctypes.byref(size), 0):
        raise RuntimeError('Windows interface discovery changed or failed')
    return buffer.raw


def host_addresses():
    """Read every current Windows IPv4 interface, including VPNs, for each connection."""
    raw = _interface_table()
    if len(raw) < ctypes.sizeof(_AddressTable):
        raise RuntimeError('Windows interface discovery was incomplete')
    count = ctypes.c_uint32.from_buffer_copy(raw).value
    offset, stride = _AddressTable.rows.offset, ctypes.sizeof(_AddressRow)
    if not count or offset + count * stride > len(raw):
        raise RuntimeError('Windows interface discovery was empty')
    return sorted({socket.inet_ntoa(raw[offset + i * stride:offset + i * stride + 4]) for i in range(count)})


class Cancelled(RuntimeError):
    pass


def _run(cmd, cancelled=None, timeout=None):
    """Run a wsl.exe command and return (returncode, stdout). It waits as long as the command takes (a busy PC can
    need minutes to start the WSL VM, 2026-11-17), unless the session is stopped or an explicit timeout passes.
    Output is read on a thread: subprocess.run(timeout=...) on Windows can wait forever for the pipe after a kill."""
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            creationflags=0x08000000)
    out = bytearray()
    reader = threading.Thread(target=lambda: out.extend(proc.stdout.read()), daemon=True)
    reader.start()
    started = time.monotonic()
    while proc.poll() is None:
        if cancelled and cancelled():
            proc.kill()
            raise Cancelled('stopped')
        if timeout is not None and time.monotonic() - started > timeout:
            proc.kill()
            raise subprocess.TimeoutExpired(cmd, timeout)
        time.sleep(.2)
    reader.join(5)
    return proc.returncode, bytes(out)


@contextmanager
def exclusive(cancelled=None):
    """Cross-process lock: never terminate the distro under another SnapLab job. Waits without a time limit
    (it was 120 s) until the other job ends or this session is stopped."""
    import msvcrt
    root = directory()
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'active.lock').open('a+b') as lock:
        if lock.tell() == 0:
            lock.write(b'0')
            lock.flush()
        while True:
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if cancelled and cancelled():
                    raise Cancelled('stopped')
                time.sleep(.25)
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


# Jobs of this SnapLab process share one running worker (2026-11-16): the first job starts it, the last one stops
# it. Before, each job held the worker alone and terminated the distribution at its end, so SnapCode sessions on WSL
# ran one at a time and a second one failed after waiting 120 s.
_shared = {'jobs': 0, 'cfg': None, 'stop': None, 'lock': None}
_shared_lock = threading.Lock()


@contextmanager
def worker(*, _record=None, _locked=False, cancelled=None):
    if _record is not None or _locked:          # setup and its checks: one private start, as before
        with _boot(_record=_record, _locked=_locked) as cfg:
            yield cfg
        return
    with _shared_lock:
        if _shared['jobs'] == 0:
            lock = exclusive(cancelled)         # other SnapLab processes wait while this one uses the worker
            lock.__enter__()
            boot = _boot(_locked=True, cancelled=cancelled)
            try:
                _shared['cfg'] = boot.__enter__()
            except BaseException:
                lock.__exit__(None, None, None)
                raise
            _shared.update(stop=boot, lock=lock)
        _shared['jobs'] += 1
        cfg = {**_shared['cfg'], 'host_addresses': host_addresses()}
    try:
        yield cfg
    finally:
        with _shared_lock:
            _shared['jobs'] -= 1
            if _shared['jobs'] == 0:
                boot, lock = _shared['stop'], _shared['lock']
                _shared.update(cfg=None, stop=None, lock=None)
                try:
                    boot.__exit__(None, None, None)
                finally:
                    lock.__exit__(None, None, None)


@contextmanager
def _boot(*, _record=None, _locked=False, cancelled=None):
    with nullcontext() if _locked else exclusive(cancelled):
        if _record is None and not ready():
            raise RuntimeError('WSL setup or isolation verification is required')
        root = directory()
        record = _record or json.loads((root / 'ready.json').read_text(encoding='utf-8'))
        # Recheck immutable helper bytes and account privileges on every boot. This first command also starts the
        # WSL VM, which took minutes while the PC was fully busy (2026-11-17); a 30 s limit failed those turns.
        check_code, check_out = _run(command('/usr/bin/python3', '-I', '-c',
            "import hashlib,json,os,pwd,pathlib; "
            "assert pathlib.Path('/opt/snaplab').stat().st_uid==0 and not pathlib.Path('/opt/snaplab').stat().st_mode & 18; "
            "m=json.loads(pathlib.Path('/opt/snaplab/manifest.json').read_text()); "
            "assert all(pathlib.Path('/opt/snaplab',n).stat().st_uid==0 and "
            "not pathlib.Path('/opt/snaplab',n).stat().st_mode & 18 and "
            "hashlib.sha256(pathlib.Path('/opt/snaplab',n).read_bytes()).hexdigest()==h for n,h in m.items()); "
            "assert pwd.getpwnam('snaplab').pw_uid!=0; print('verified')"), cancelled)
        if check_code or check_out.strip() != b'verified':
            raise RuntimeError('WSL integrity check failed')
        daemon = subprocess.Popen(command('/usr/bin/python3', '-I', '/opt/snaplab/wsl_sshd.py'),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=0x08000000)
        try:
            port = int(record['ssh_port'])
            while True:                         # until SSH answers, its daemon ends, or the session is stopped
                if daemon.poll() is not None:
                    raise RuntimeError('WSL resource limits or SSH startup failed')
                if cancelled and cancelled():
                    raise Cancelled('stopped')
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=.2):
                        break
                except OSError:
                    time.sleep(.1)
            yield {'host': '127.0.0.1', 'user': 'snaplab', 'ssh_port': port,
                   'ssh_exe': str(Path(os.environ['SYSTEMROOT']) / 'System32/OpenSSH/ssh.exe'),
                   'codex_bin': '/usr/local/bin/codex',
                   'identity': str(root / 'identity'), 'known_hosts': str(root / 'known_hosts'),
                   'host_addresses': host_addresses(),
                   'desktop': True, 'wsl': True}
        finally:
            try:
                code, out = _run(command('/usr/bin/python3', '-I', '-c',
                    "import pathlib,json; p=pathlib.Path('/sys/fs/cgroup/snaplab-worker'); "
                    "print(json.dumps({n:(p/n).read_text().strip() for n in "
                    "('memory.peak','memory.max','pids.peak','pids.max','cpu.stat','cpu.max')}))"), timeout=60)
                if code == 0:
                    (root / 'last-run-metrics.json').write_text(json.dumps(json.loads(out)), encoding='utf-8')
            except (OSError, ValueError, subprocess.SubprocessError):
                pass  # Observability must not prevent teardown.
            daemon.terminate()
            try:
                daemon.wait(timeout=10)
            except subprocess.TimeoutExpired:
                daemon.kill()
            try:
                _run(['wsl.exe', '--terminate', DISTRO], timeout=120)
            except (OSError, subprocess.SubprocessError):
                pass


def run_turn(app, session, folder, prompt, ctl):
    from .remote import run_turn as remote_turn
    try:
        with worker(cancelled=ctl.cancelled) as cfg:
            if ctl.cancelled():
                return TurnResult(ok=False, error='stopped')
            return remote_turn(app, session, folder, prompt, ctl, _cfg=cfg)
    except Cancelled:
        return TurnResult(ok=False, error='stopped')
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return TurnResult(ok=False, error='WSL worker failed its startup or isolation checks; work was blocked.')
