"""Standalone Linux worker and bounded file protocol. Sent as code, never installed globally."""
from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager

# Sizes the file protocol accepts. They were 20 MB a file, 100 MB in all and 5000 files, which stopped real
# projects (game assets, textures); now only memory bounds them (files are held in memory while they move).
MAX_FILE = 4 * 1024 ** 3
MAX_TOTAL = 64 * 1024 ** 3
MAX_FILES = 10 ** 7
MAX_LINE = (MAX_FILE * 4 // 3) + 65536
SKIP = {".git", ".codex", ".pi", ".ssh", ".aws", ".worktrees", "node_modules", "__pycache__",
        ".venv", "venv", ".pytest_cache", ".mypy_cache", ".npmrc", ".netrc", "_netrc",
        ".pypirc", ".git-credentials", ".config", ".azure", ".kube", ".gnupg",
        "config.json", "auth.json", "credentials", "credentials.json", "data", "runtime"}
META = {"PLAN.md", "KNOWLEDGE.md", "attachments.json"}


def skipped_component(parts, index):
    part = parts[index].lower()
    # Game assets are allowed only under this explicit source directory.
    source_data = index == 1 and parts[0].lower() == "src" and part == "data"
    return (part in SKIP and not source_data) or part.startswith(".env") or part == "state.md"


def file_name(name):
    if not isinstance(name, str) or not name or len(name) > 400:
        raise ValueError("invalid transfer path")
    parts = name.split("/")
    if any(p in ("", ".", "..") or p.endswith((".", " ")) or
           re.search(r'[\\:\x00-\x1f<>"|?*]', p) or
           re.fullmatch(r"(?i)(con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\..*)?", p) for p in parts):
        raise ValueError("invalid transfer path")
    if any(skipped_component(parts, i) for i in range(len(parts))):
        raise ValueError("private or dependency path")
    if any(p.lower() == ".snaplab" for p in parts):
        if len(parts) != 2 or parts[0] != ".snaplab" or parts[1] not in META:
            raise ValueError("private metadata path")
    return name


def safe_path(root, name):
    file_name(name)
    root = Path(os.path.abspath(root))
    path = root.joinpath(*name.split("/"))
    for part in (root, *[root.joinpath(*name.split("/")[:i]) for i in range(1, len(name.split("/")) + 1)]):
        if part.is_symlink():
            raise ValueError("linked transfer path")
        if part.exists():
            info = part.lstat()
            if getattr(info, "st_file_attributes", 0) & 0x400 or (part.is_file() and info.st_nlink != 1):
                raise ValueError("linked transfer path")
    if path.resolve() != path or not path.is_relative_to(root):
        raise ValueError("transfer path escaped workspace")
    return path


def read_file(root, name):
    path = safe_path(root, name)
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_FILE:
            raise ValueError("file is not regular or too large")
        if Path("/proc/self/fd").exists() and Path(os.readlink(f"/proc/self/fd/{stream.fileno()}")) != path:
            raise ValueError("transfer path changed")
        raw = stream.read(before.st_size + 1)       # read(n) reserves n bytes first: size it by the file
        after = path.stat()
        if len(raw) > MAX_FILE or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError("file changed during transfer")
    safe_path(root, name)
    return raw


def write_file(root, name, raw):
    path = safe_path(root, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_path(root, name)
    fd, tmp = tempfile.mkstemp(prefix=".transfer-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
        safe_path(root, name)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def inventory(root, linked=None):
    """The transferable files of a workspace. A linked directory (a symlink or junction) is not entered and nothing
    in it is transferred; its path is added to `linked` when a list is given. 2026-11-21: a cloned repository with
    one symlinked folder (The Graph's packages/contracts-test/contracts) used to stop every turn of its session."""
    names = []
    for here, dirs, files in os.walk(root, followlinks=False):
        parts = Path(here).relative_to(root).parts
        dirs[:] = sorted(d for d in dirs if not skipped_component((*parts, d), len(parts)))
        if Path(here).relative_to(root).parts[:1] == (".snaplab",):
            dirs[:] = []
        keep = []
        for d in dirs:
            p = Path(here, d)
            if p.is_symlink() or getattr(p.lstat(), "st_file_attributes", 0) & 0x400:
                if linked is not None:
                    linked.append(p.relative_to(root).as_posix())
                continue
            keep.append(d)
        dirs[:] = keep
        for name in sorted(files):
            rel = Path(here, name).relative_to(root).as_posix()
            try:
                file_name(rel)
            except ValueError:
                continue
            names.append(rel)
            if len(names) > MAX_FILES:
                raise ValueError(f"workspace exceeds {MAX_FILES} files")
    return names


# Each job keeps its folder between turns. A manifest (path -> SHA-256) lets both sides move only what changed
# (2026-11-17): every turn used to send the whole workspace both ways (27,600 files, 383 MB for one game project) and
# the home PC rewrote every file, which took 46-74 minutes after each turn.
CACHE_DIR = Path('.local/state/snaplab-manifest')
DIGEST = re.compile(r'[0-9a-f]{64}')
# File times advance in steps, so a file changed less than this before it was read could change again with the same
# times: such an entry is always read again (git's "racily clean" rule).
RACY_NS = 2_000_000_000


def cached(entry, key):
    """The digest a cache entry [key, digest, read_at] gives for a file with this stat key, or None."""
    if (isinstance(entry, list) and len(entry) == 3 and entry[0] == key and isinstance(entry[1], str)
            and isinstance(entry[2], int) and max(key[1], key[2]) < entry[2] - RACY_NS):
        return entry[1]
    return None


def open_regular(root, name):
    """Open root/name for reading if it is a regular, unlinked file, without following a link in its last part
    (a FIFO or device cannot block: O_NONBLOCK). Used for the job's own folder, whose directories inventory()
    has checked for links, and for earlier jobs, whose content is accepted only when its SHA-256 matches.
    safe_path() checks every directory again for each file, which made copying 27,600 files take 30 s."""
    try:
        fd = os.open(root.joinpath(*file_name(name).split('/')),
                     os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_CLOEXEC', 0))
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise ValueError('linked transfer path') from None
        raise
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(fd)
        raise ValueError('file is not regular')
    return fd, info


def read_regular(root, name):
    fd, _ = open_regular(root, name)
    with os.fdopen(fd, 'rb') as stream:
        return stream.read()


def through_link(name, linked):
    return any(name == d or name.startswith(d + '/') for d in linked)


def write_own(root, name, raw, made, linked=()):
    """Write a file into the job's own folder during sync: no other process writes there then (the job lock is
    held), and the path does not pass through a linked directory that inventory() found. The new file replaces the
    name atomically, and a link in the last part is replaced, never followed."""
    if through_link(name, linked):
        raise ValueError('linked transfer path: ' + name)
    path = root.joinpath(*file_name(name).split('/'))
    if path.parent not in made:
        path.parent.mkdir(parents=True, exist_ok=True)
        made.add(path.parent)
    fd, tmp = tempfile.mkstemp(prefix='.transfer-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def hash_file(root, name):
    """SHA-256 of one transferable file, read in blocks. Returns (digest, stat key)."""
    fd, before = open_regular(root, name)
    digest = hashlib.sha256()
    with os.fdopen(fd, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest(), [before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_ino]


def load_cache(home, sid):
    try:
        data = json.loads((home / CACHE_DIR / f'{sid}.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_cache(home, sid, cache):
    folder = home / CACHE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f'.{sid}.{os.getpid()}.tmp'
    tmp.write_text(json.dumps(cache), encoding='utf-8')
    os.replace(tmp, folder / f'{sid}.json')


def manifest(root, cache, linked=None):
    """path -> SHA-256 for every transferable file. A file whose size, times and inode match the cache is not read
    again; the change time (ctime) cannot be set by a program, so a rewritten file is always read."""
    out, fresh = {}, {}
    now = time.time_ns()
    for name in inventory(root, linked):
        try:
            info = root.joinpath(*name.split('/')).lstat()
        except OSError:
            continue
        key = [info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino]
        digest = cached(cache.get(name), key)
        read_at = cache[name][2] if digest else now
        if digest is None:
            digest, key = hash_file(root, name)
        out[name] = digest
        fresh[name] = [key, digest, read_at]
    return out, fresh


def seed_digests(root, cache):
    """path -> SHA-256 of an earlier job's folder, from its cache where it still matches and by reading the rest.
    That job may be running: a file that cannot be read cleanly is left out (every copy is checked again)."""
    out = {}
    try:
        names = inventory(root)
    except (OSError, ValueError):
        return out
    for name in names:
        try:
            info = root.joinpath(*name.split('/')).lstat()
            key = [info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino]
            out[name] = cached(cache.get(name), key) or hash_file(root, name)[0]
        except (OSError, ValueError):
            continue
    return out


def remove_empty_parents(root, name):
    parent = root.joinpath(*name.split('/')).parent
    while parent != root and parent.is_relative_to(root):
        try:
            parent.rmdir()
        except OSError:
            return
        parent = parent.parent


def sync(req, folder, home):
    """Make the job folder equal the home PC's manifest. Files the worker already has (here, or in an earlier job of
    the same project) are copied locally; only the rest are requested. Returns (wanted manifest, cache)."""
    started = time.monotonic()
    sid = req['sid']
    wanted = req['manifest']
    if not isinstance(wanted, dict):
        raise ValueError('invalid manifest')
    for name, digest in wanted.items():
        file_name(name)
        if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
            raise ValueError('invalid manifest')
    linked = []
    current, cache = manifest(folder, load_cache(home, sid), linked)
    # Preserve the files of a disconnected or stopped turn that never reached the home PC before replacing them.
    # The recovery directory is outside every job mount and never uploaded as context.
    lost = [n for n in current if wanted.get(n) != current[n]]
    if any(not n.startswith('.snaplab/') for n in lost):
        recovery = Path(tempfile.mkdtemp(prefix=folder.name + '-recovery-', dir=folder.parent))
        for name in lost:
            write_file(recovery, name, read_file(folder, name))
        emit({'kind': 'recovery', 'name': recovery.name})
    missing = {n: h for n, h in wanted.items() if current.get(n) != h}
    sources = {}
    for name, digest in current.items():
        sources.setdefault(digest, (folder, name))
    seeds = [x for x in req.get('seeds') or [] if isinstance(x, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,40}', x) and x != sid]
    stage = Path(tempfile.mkdtemp(prefix=folder.name + '-sync-', dir=folder.parent))
    staged, made = {}, set()
    try:
        for seed in [None, *seeds]:
            if seed is not None:
                if all(h in sources for h in missing.values()):
                    break
                seed_folder = folder.parent / seed
                if seed_folder.is_symlink() or not seed_folder.is_dir():
                    continue
                for name, digest in seed_digests(seed_folder, load_cache(home, seed)).items():
                    sources.setdefault(digest, (seed_folder, name))
            for name, digest in missing.items():
                if name in staged or digest not in sources:
                    continue
                try:
                    raw = read_regular(*sources[digest])
                except (OSError, ValueError):
                    continue
                if hashlib.sha256(raw).hexdigest() == digest:
                    staged[name] = stage / str(len(staged))
                    staged[name].write_bytes(raw)
        for name in current:
            if name not in wanted:
                safe_path(folder, name).unlink()
                remove_empty_parents(folder, name)
        for name, tmp in staged.items():
            write_own(folder, name, tmp.read_bytes(), made, linked)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    need = sorted(n for n in missing if n not in staged)
    emit({'kind': 'need', 'paths': need, 'local': len(staged), 'seconds': round(time.monotonic() - started, 1)})
    pending = set(need)
    while True:
        line = sys.stdin.buffer.readline(MAX_LINE + 1)
        if len(line) > MAX_LINE or not line:
            raise ValueError('incomplete upload')
        item = json.loads(line)
        if item.get('kind') == 'start':
            break
        name = file_name(item['path'])
        if name not in pending:
            raise ValueError('unrequested upload')
        raw = base64.b64decode(item['data'], validate=True)
        if hashlib.sha256(raw).hexdigest() != wanted[name]:
            raise ValueError('upload does not match its manifest')
        write_own(folder, name, raw, made, linked)
        pending.discard(name)
    if pending:
        raise ValueError('incomplete upload')
    _, cache = manifest(folder, cache)
    save_cache(home, sid, cache)
    return wanted, cache


EMIT_LOCK = threading.Lock()


def emit(event):
    with EMIT_LOCK:
        print(json.dumps(event, ensure_ascii=True), flush=True)


def watch_plan(folder, ended, every=3.0):
    """Send .snaplab/PLAN.md whenever it changes, so SnapLab can show how far the turn has got while it runs."""
    seen = None
    path = Path(folder) / ".snaplab" / "PLAN.md"
    while not ended.wait(every):
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 64000:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if text.strip() and text != seen:          # skip the empty moment of a rewrite
            seen = text
            emit({"kind": "plan", "text": text})


OWNER = None
OWNER_DATA = {}


def record_process(proc=None):
    if OWNER is None:
        return
    if proc is not None:
        try:
            start = Path('/proc', str(proc.pid), 'stat').read_text().rsplit(')', 1)[1].split()[19]
            OWNER_DATA.setdefault('groups', []).append({'pid': proc.pid, 'start': start})
        except (OSError, IndexError):
            pass
    temp = OWNER.with_suffix('.tmp')
    temp.write_text(json.dumps(OWNER_DATA), encoding='utf-8')
    temp.replace(OWNER)


def stop_group(proc):
    if proc is not None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


# What a new desktop job is expected to need when it starts (agent, display, a Chrome window with DevTools):
# measured on a Linux worker at about 570 MiB and 233 tasks (threads included).
JOB_START_MB = 1024
JOB_START_TASKS = 300


def limited_parents():
    """This process's cgroup and its parents (the worker account's slice has the account-wide limits)."""
    line = Path('/proc/self/cgroup').read_text().splitlines()[0]
    group = Path('/sys/fs/cgroup') / line.split(':', 2)[2].lstrip('/')
    return [g for g in (group, *group.parents) if g != Path('/sys/fs/cgroup')]


def memory_free_mb():
    """Memory a new job can use: the host's available memory, and the headroom of any limited parent cgroup
    (the worker account's slice), not counting file cache that the kernel can drop."""
    free = int(re.search(r'MemAvailable:\s+(\d+)', Path('/proc/meminfo').read_text())[1]) // 1024
    try:
        for g in limited_parents():
            if not (g / 'memory.max').exists():
                continue
            cap = (g / 'memory.max').read_text().strip()
            if cap == 'max':
                continue
            stat = dict(row.split() for row in (g / 'memory.stat').read_text().splitlines() if len(row.split()) == 2)
            used = int((g / 'memory.current').read_text()) - int(stat.get('inactive_file', 0)) - int(stat.get('active_file', 0))
            free = min(free, (int(cap) - used) // 1048576)
    except (OSError, ValueError, IndexError, TypeError):
        pass
    return free


def tasks_free():
    """Free task slots (processes and threads) under the tightest parent limit; None when nothing limits them."""
    free = None
    try:
        for g in limited_parents():
            cap = (g / 'pids.max').read_text().strip() if (g / 'pids.max').exists() else 'max'
            if cap != 'max':
                left = int(cap) - int((g / 'pids.current').read_text())
                free = left if free is None else min(free, left)
    except (OSError, ValueError, IndexError):
        pass
    return free


@contextmanager
def desktop_slot(home, cancel, need_mb=JOB_START_MB, notify=None):
    """Start a job when the worker has memory and task slots for it, and wait (with no time limit) until it has.

    There is no fixed number of desktops. Jobs admitted during the last minute count with their expected size,
    because their memory has not grown yet; a job's lock file is released when it ends, also on a crash.
    """
    import fcntl
    locks = home / '.local/state/snaplab-locks'
    locks.mkdir(parents=True, exist_ok=True)
    mine = locks / f'desktop-{os.getpid()}-{os.urandom(4).hex()}'
    held = None
    told = False
    try:
        while held is None:
            if cancel.is_set():
                raise RuntimeError('cancelled while waiting for memory on the worker')
            with (locks / 'admission').open('a+') as gate:
                fcntl.flock(gate, fcntl.LOCK_EX)
                reserved = reserved_tasks = 0
                now = time.time()
                for other in locks.glob('desktop-*'):
                    try:
                        with other.open('r') as probe:
                            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        other.unlink(missing_ok=True)          # nobody holds it: that job has ended
                        continue
                    except BlockingIOError:
                        pass
                    except OSError:
                        continue
                    try:
                        info = json.loads(other.read_text(encoding='utf-8') or '{}')
                    except (OSError, ValueError):
                        info = {}
                    if now - float(info.get('started', 0)) < 60:
                        reserved += int(info.get('need_mb', need_mb))
                        reserved_tasks += JOB_START_TASKS
                free = memory_free_mb() - reserved
                tasks = tasks_free()
                if free >= need_mb and (tasks is None or tasks - reserved_tasks >= JOB_START_TASKS):
                    held = mine.open('w')
                    fcntl.flock(held, fcntl.LOCK_EX)
                    held.write(json.dumps({'need_mb': need_mb, 'started': now}))
                    held.flush()
            if held is None:
                if notify and not told:
                    notify(free, need_mb, None if tasks is None else tasks - reserved_tasks)
                    told = True
                cancel.wait(2)
        yield
    finally:
        if held:
            held.close()
            mine.unlink(missing_ok=True)


def waiting(free_mb, need_mb, free_tasks=None):
    emit({'kind': 'waiting', 'free_mb': int(free_mb), 'need_mb': int(need_mb), 'free_tasks': free_tasks})


def limited_command(cmd, memory_mb=3072):
    """One job's limits. OOMPolicy=continue: when the kernel kills one process at the limit (often a browser
    tab, which Chrome shows as crashed), the rest of the job carries on instead of the whole turn being
    stopped with SIGTERM (exit -15)."""
    if os.environ.get('SNAPLAB_WSL') == '1':
        return cmd  # The root SSH supervisor limits the entire WSL worker cgroup.
    return ['systemd-run', '--user', '--scope', '--quiet', '-p', 'OOMPolicy=continue',
            '-p', f'MemoryMax={memory_mb}M', '-p', 'MemorySwapMax=0', '-p', 'TasksMax=1024', '--', *cmd]


def isolated_command(cmd, folder, home, codex_home):
    """Only this workspace is writable; other homes and host processes do not exist here.

    Bubblewrap's PID namespace and parent-death handling also kill daemonized descendants.
    Network is shared for the SSH reverse forward. Each relay still requires its own token.
    Missing/unsupported isolation fails closed; there is no unsandboxed fallback.
    """
    args = ["/usr/bin/bwrap", "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
            "--unshare-cgroup", "--die-with-parent", "--new-session", "--cap-drop", "ALL",
            "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin",
            "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--dir", str(home)]
    for name in ("resolv.conf", "hosts", "nsswitch.conf", "passwd", "group", "ssl", "fonts", "localtime", "libreoffice"):
        path = "/etc/" + name
        if Path(path).exists():
            args += ["--ro-bind", path, path]
    for path in (home / ".local/bin", home / ".local/lib/node_modules", Path("/opt/playwright-browsers")):
        if path.exists():
            args += ["--ro-bind", str(path), str(path)]
    args += ["--bind", str(folder), str(folder)]
    acceptance = folder / 'tests' / 'acceptance'
    if (Path(codex_home) / 'protect_acceptance').exists() and acceptance.is_dir():
        args += ['--ro-bind', str(acceptance), str(acceptance)]
    protected_tools = folder / 'tools'
    if (Path(codex_home) / 'protect_acceptance').exists() and protected_tools.is_dir():
        args += ['--ro-bind', str(protected_tools), str(protected_tools)]
    result = args + ["--ro-bind", str(codex_home), "/codex-config",
                     "--chdir", str(folder), "--", *cmd]
    if os.environ.get('SNAPLAB_WSL') == '1':
        return ['/usr/bin/python3', '-I', '/opt/snaplab/wsl_runtime.py', *result]
    return result


def execute(cmd, folder, env, prompt, cancel, timeout, *, events=False, harness="codex"):
    try:
        proc = subprocess.Popen(cmd, cwd=folder, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, start_new_session=True)
    except OSError:
        return None, "The process could not be started."
    record_process(proc)
    ended = threading.Event()
    expired = threading.Event()
    start = time.monotonic()

    def watch():
        while not ended.wait(0.2):
            if cancel.is_set() or time.monotonic() - start > timeout:
                expired.set()
                stop_group(proc)
                return
    threading.Thread(target=watch, daemon=True).start()
    if events:
        threading.Thread(target=watch_plan, args=(folder, ended), daemon=True).start()
    tail = ""
    try:
        proc.stdin.write(prompt.encode("utf-8"))
        proc.stdin.close()
        for line in iter(lambda: proc.stdout.readline(1024 * 1024), b""):
            text = line.decode("utf-8", "replace")
            if events:
                try:
                    ev = json.loads(text)
                except ValueError:
                    continue  # stderr may contain credentials; do not forward it
                if isinstance(ev, dict):
                    if ev.get('type') == 'snaplab.screen':
                        emit({'kind': 'screen', 'data': ev.get('data')})
                    else:
                        emit({"kind": harness, "event": ev})
            else:
                tail = (tail + text)[-64000:]
        proc.wait()
        return (124 if expired.is_set() and not cancel.is_set() else None if expired.is_set() else proc.returncode), tail
    finally:
        ended.set()
        stop_group(proc)  # includes children left running after Codex exits
        proc.wait()
        proc.stdout.close()
        if not proc.stdin.closed:
            proc.stdin.close()


def main():
    os.umask(0o077)
    first = sys.stdin.buffer.readline(MAX_LINE)
    req = json.loads(first)
    sid = req["sid"]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", sid):
        raise ValueError("bad session id")
    home = Path.home()
    if req.get("mode") == "live":
        return live(req, home)
    jobs = home / "jobs"
    folder = jobs / sid
    jobs.mkdir(exist_ok=True)
    if jobs.is_symlink() or jobs.resolve() != jobs or folder.is_symlink():
        raise ValueError("invalid jobs directory")
    folder.mkdir(exist_ok=True)
    # One process owns a job until all its files have been exported.
    import fcntl
    locks = home / ".local" / "state" / "snaplab-locks"
    locks.mkdir(parents=True, exist_ok=True)
    with (locks / sid).open("w") as lock:
        # SnapLab's browser panel may still be closing its own browser in this job (remote_worker.live, host mode):
        # wait for it a little instead of failing the turn.
        waited = time.monotonic()
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() - waited > 90:
                    raise
                time.sleep(0.5)
        global OWNER, OWNER_DATA
        OWNER = locks / (sid + '.owner.json')
        OWNER_DATA = {'pid': os.getpid(), 'start': Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19], 'groups': []}
        record_process()
        return run(req, folder, home)


def run(req, folder, home):
    # Reset transferable files to the home PC's manifest; keep installed dependencies.
    wanted, cache = sync(req, folder, home)
    meta = folder / ".snaplab"
    meta.mkdir(exist_ok=True)
    for name, raw in req["helpers"].items():
        if name not in ("capture.py", "capture.mjs", "browser.mjs", "keep.py", "desktop.py", "adapters.py"):
            raise ValueError("invalid helper")
        target = meta / name
        if target.is_symlink() or (target.exists() and target.stat().st_nlink != 1):
            raise ValueError("linked helper")
        target.write_text(raw, encoding="utf-8")
    cancel = threading.Event()
    watch_done = threading.Event()

    def disconnected():
        # Raw fd polling avoids leaving a daemon holding Python's stdin lock at shutdown.
        while not watch_done.is_set():
            readable, _, _ = select.select([sys.stdin.fileno()], [], [], 0.2)
            if readable:
                os.read(sys.stdin.fileno(), 1)  # STOP byte or SSH EOF
                cancel.set()
                return
    watcher = threading.Thread(target=disconnected, daemon=True)
    watcher.start()
    # The token lives only in memory/environment. Never persist a Codex transcript.
    with tempfile.TemporaryDirectory(prefix="snaplab-codex-") as codex_home:
        config = req["config"]
        Path(codex_home, "config.toml").write_text(config, encoding="utf-8")
        Path(codex_home, "AGENTS.md").write_text(req["instructions"], encoding="utf-8")
        # Config is read-only, while Codex's disposable state is inside the private /tmp.
        env = {"HOME": str(home), "PATH": str(home / ".local/bin") + ":/usr/local/bin:/usr/bin:/bin",
               "LANG": "C.UTF-8", "CODEX_HOME": "/tmp/codex", "SNAPLAB_PROXY_TOKEN": req.pop("token"),
               "PLAYWRIGHT_BROWSERS_PATH": "/opt/playwright-browsers", "PIP_BREAK_SYSTEM_PACKAGES": "1"}
        if os.environ.get('SNAPLAB_WSL') == '1':
            env['SNAPLAB_EGRESS_TOKEN'] = env['SNAPLAB_PROXY_TOKEN']
            env['SNAPLAB_RELAY_PORT'] = str(int(req['relay_port']))
            env['SNAPLAB_HOST_ADDRESSES'] = json.dumps(req.get('host_addresses', []))
            env['SNAPLAB_MODEL_RELAY_REQUIRED'] = '1'
        import importlib.util
        adapter_path = Path(codex_home, 'adapters.py')
        adapter_path.write_text(req['helpers']['adapters.py'], encoding='utf-8')
        spec = importlib.util.spec_from_file_location('worker_adapters', adapter_path)
        adapters = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(adapters)
        cmd = adapters.launch(req, home, folder, codex_home)
        env.update(PI_CODING_AGENT_DIR='/tmp/codex', PI_TELEMETRY='0', DO_NOT_TRACK='1',
                   SNAPLAB_WEB_TOKEN=req.pop('web_token', ''), NODE_OPTIONS='--max-old-space-size=640', PYTHONDONTWRITEBYTECODE='1')
        # keep.py supervises the agent so processes it keeps (servers, a live browser) survive each tool call
        # and end with the turn. It lives in the read-only configuration mount, like desktop.py.
        keep = 'python3 /codex-config/keep.py --supervise ' if req['helpers'].get('keep.py') else ''
        if keep:
            Path(codex_home, 'keep.py').write_text(req['helpers']['keep.py'], encoding='utf-8')
        launch = ["/bin/sh", "-c", 'mkdir -p /tmp/codex; cp /codex-config/* /tmp/codex/; exec ' + keep + '"$@"', "snaplab", *cmd]
        if req.get('desktop', False):
            if re.fullmatch(r'\d{3,4}x\d{3,4}', str(req.get('screen') or '')):
                env['SNAPLAB_SCREEN'] = req['screen']
            env.update(XDG_RUNTIME_DIR=f'/run/user/{os.getuid()}',
                       DBUS_SESSION_BUS_ADDRESS=f'unix:path=/run/user/{os.getuid()}/bus')
            # The helper itself is copied into the read-only configuration mount.
            Path(codex_home, 'desktop.py').write_text(req['helpers']['desktop.py'], encoding='utf-8')
            launch = ['/bin/sh', '-c', 'mkdir -p /tmp/codex; cp /codex-config/* /tmp/codex/; exec ' + keep + 'python3 /codex-config/desktop.py --supervise "$@"', 'snaplab', *cmd]
            with desktop_slot(home, cancel, notify=waiting):
                code, _ = execute(limited_command(isolated_command(launch, folder, home, codex_home)),
                                  folder, env, req['prompt'], cancel, req['timeout'], events=True, harness=req.get("harness", "codex"))
        else:
            command = isolated_command(launch, folder, home, codex_home)
            if req.get('harness') == 'pi':
                env.update(XDG_RUNTIME_DIR=f'/run/user/{os.getuid()}', DBUS_SESSION_BUS_ADDRESS=f'unix:path=/run/user/{os.getuid()}/bus')
                command = limited_command(command)
            code, _ = execute(command, folder, env,
                              req["prompt"], cancel, req["timeout"], events=True, harness=req.get("harness", "codex"))
        if code == 0 and req.get("test_command") and not cancel.is_set():
            test_env = {k: v for k, v in env.items() if k not in ("SNAPLAB_PROXY_TOKEN", "SNAPLAB_WEB_TOKEN", "CODEX_HOME")}
            command = isolated_command(["/bin/bash", "-c", req["test_command"]],folder,home,codex_home)
            memory = 3072
            if req.get('desktop', False):
                with desktop_slot(home, cancel, notify=waiting):
                    test_code, output = execute(limited_command(command, memory),folder,test_env,'',cancel,req['test_timeout'])
            else:
                test_code, output = execute(limited_command(command, memory),folder,test_env,'',cancel,req['test_timeout'])
            emit({"kind": "tests", "exit_code": test_code, "output": output})
        env.clear()
    if cancel.is_set():
        watch_done.set()
        watcher.join(timeout=1)
        return 1
    # Return only what differs from the home PC's copy, and the names of the files that are gone.
    linked = []
    final, cache = manifest(folder, cache, linked)
    save_cache(home, req['sid'], cache)
    if linked:
        emit({"kind": "linked", "paths": sorted(linked)[:50]})
    total = 0
    for name in sorted(final):
        if wanted.get(name) == final[name]:
            continue
        raw = read_file(folder, name)
        total += len(raw)
        if total > MAX_TOTAL:
            raise ValueError("changed files exceed the transfer limit")
        emit({"kind": "file", "path": name, "data": base64.b64encode(raw).decode("ascii")})
    emit({"kind": "deleted", "paths": sorted(n for n in wanted if n not in final)})
    emit({"kind": "complete", "exit_code": code})
    watch_done.set()
    watcher.join(timeout=1)
    return 0


# ---------------------------------------------------------------- SnapLab's browser panel

LIVE_HELPERS = ("capture.py", "capture.mjs", "browser.mjs", "keep.py", "desktop.py")


def write_helpers(folder, helpers, names):
    meta = folder / ".snaplab"
    if meta.is_symlink():
        raise ValueError("linked helper folder")
    meta.mkdir(exist_ok=True)
    for name, raw in helpers.items():
        if name not in names:
            raise ValueError("invalid helper")
        target = meta / name
        if target.is_symlink() or (target.exists() and target.stat().st_nlink != 1):
            raise ValueError("linked helper")
        target.write_text(raw, encoding="utf-8")


def host_job(req, folder, home, stop):
    """The browser alone, between turns: the same sandbox and private desktop as a turn, running
    `browser.mjs host` (a Chrome window with the job's profile) until SnapLab disconnects or Chrome closes."""
    code = None
    try:
        write_helpers(folder, req["helpers"], LIVE_HELPERS)
        with tempfile.TemporaryDirectory(prefix="snaplab-browser-") as codex_home:
            for name in ("keep.py", "desktop.py"):
                Path(codex_home, name).write_text(req["helpers"][name], encoding="utf-8")
            env = {"HOME": str(home), "PATH": str(home / ".local/bin") + ":/usr/local/bin:/usr/bin:/bin",
                   "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
                   "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}",
                   "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{os.getuid()}/bus"}
            if re.fullmatch(r"\d{3,4}x\d{3,4}", str(req.get("screen") or "")):
                env["SNAPLAB_SCREEN"] = req["screen"]
            if os.environ.get("SNAPLAB_WSL") == "1":
                env["SNAPLAB_EGRESS_TOKEN"] = req["token"]
                env["SNAPLAB_RELAY_PORT"] = str(int(req["relay_port"]))
                env["SNAPLAB_HOST_ADDRESSES"] = json.dumps(req.get("host_addresses", []))
            launch = ["/bin/sh", "-c", "mkdir -p /tmp/codex; cp /codex-config/* /tmp/codex/; exec python3 "
                      "/codex-config/keep.py --supervise python3 /codex-config/desktop.py --supervise \"$@\"",
                      "snaplab", "node", str(folder / ".snaplab" / "browser.mjs"), "host"]
            with desktop_slot(home, stop, notify=waiting):
                code, _ = execute(limited_command(isolated_command(launch, folder, home, codex_home)),
                                  folder, env, "", stop, 7 * 24 * 3600, events=True, harness="browser")
    except Exception as exc:  # noqa: BLE001 - reported as a kind, never with details
        emit({"kind": "host_failed", "error_type": type(exc).__name__})
    finally:
        emit({"kind": "host_ended", "exit_code": code})
        stop.set()                              # here: the host job's own cancel event


def connect_live(path, stop, ask, timeout):
    """Connect to the live browser's socket in the job folder. ask: a turn is running; if it has no browser yet,
    ask its desktop supervisor to start one (.snaplab/browser-request)."""
    import socket
    started = time.monotonic()
    asked = False
    while not stop.is_set() and time.monotonic() - started < timeout:
        try:
            info = path.lstat()
            if stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid():
                conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    conn.connect(str(path))
                    return conn
                except OSError:
                    conn.close()
        except OSError:
            pass
        if ask and not asked and time.monotonic() - started > 2 and path.parent.is_dir() and not path.parent.is_symlink():
            try:
                os.close(os.open(path.parent / "browser-request", os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                                 getattr(os, "O_NOFOLLOW", 0), 0o600))
            except OSError:
                pass
            asked = True
        stop.wait(0.3)
    return None


def pump(conn, stop):
    """DevTools lines both ways: SnapLab's commands from stdin to the browser, its answers and events to stdout.
    Returns "eof" when SnapLab closed the connection, "socket" when the browser side did."""
    closed_by = {"who": "socket"}

    def from_browser():
        try:
            for line in conn.makefile("rb"):
                if not line.startswith(b"{"):
                    continue
                with EMIT_LOCK:
                    sys.stdout.buffer.write(b'{"kind":"cdp","m":' + line.rstrip(b"\r\n") + b"}\n")
                    sys.stdout.buffer.flush()
        except (OSError, ValueError):
            pass
        finally:
            emit({"kind": "disconnected"})
            stop.set()
    reader = threading.Thread(target=from_browser, daemon=True)
    reader.start()
    pending = b""
    try:
        while not stop.is_set():
            readable, _, _ = select.select([sys.stdin.fileno()], [], [], 0.2)
            if not readable:
                continue
            chunk = os.read(sys.stdin.fileno(), 1 << 16)
            if not chunk:
                closed_by["who"] = "eof"                # SnapLab closed the panel's connection
                break
            pending += chunk
            if len(pending) > 16 * 1024 * 1024:
                raise ValueError("oversized message")
            *lines, pending = pending.split(b"\n")
            for line in lines:
                if line.strip():
                    conn.sendall(line + b"\n")
    finally:
        stop.set()
        try:
            conn.close()
        except OSError:
            pass
        reader.join(timeout=2)
    return closed_by["who"]


def live(req, home):
    """SnapLab's browser panel. attach: the running turn's live browser (started on request when it has none).
    host: no turn runs, so run the browser alone in the job's sandbox (the job lock keeps a turn out meanwhile)."""
    import fcntl
    sid = req["sid"]
    jobs = home / "jobs"
    folder = jobs / sid
    stop = threading.Event()                    # the panel's connection
    cancel = threading.Event()                  # the host job (kept separate: Chrome must finish writing first)
    job = lock = None
    try:
        if req.get("host"):
            jobs.mkdir(exist_ok=True)
            if jobs.is_symlink() or jobs.resolve() != jobs or folder.is_symlink():
                raise ValueError("invalid jobs directory")
            folder.mkdir(exist_ok=True)
            locks = home / ".local" / "state" / "snaplab-locks"
            locks.mkdir(parents=True, exist_ok=True)
            lock = (locks / sid).open("w")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                emit({"kind": "busy"})                  # a turn holds the job: attach to it instead
                return 0
            job = threading.Thread(target=host_job, args=(req, folder, home, cancel), daemon=True)
            job.start()
        elif folder.is_symlink() or not folder.is_dir():
            emit({"kind": "nojob"})
            return 0
        else:
            # No turn holds the job (it ended, or has not started): SnapLab runs the browser itself instead.
            locks = home / ".local" / "state" / "snaplab-locks"
            if not (locks / sid).exists():
                emit({"kind": "noturn"})
                return 0
            with (locks / sid).open("a") as probe:
                try:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(probe, fcntl.LOCK_UN)
                    emit({"kind": "noturn"})
                    return 0
                except BlockingIOError:
                    pass
        conn = connect_live(folder / ".snaplab" / "live.sock", cancel if job else stop, ask=not req.get("host"),
                            timeout=float(req.get("wait_s") or 120))
        if conn is None:
            emit({"kind": "nobrowser"})
            return 0
        emit({"kind": "connected", "host": bool(req.get("host"))})
        if pump(conn, stop) == "socket" and job is not None:
            job.join(timeout=40)                # the browser closed (Browser.close): its job ends by itself
        return 0
    finally:
        stop.set()
        cancel.set()
        if job is not None:
            job.join(timeout=30)
        if lock is not None:
            lock.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        # Do not expose request contents, environment values or remote tracebacks.
        import traceback
        frames = traceback.extract_tb(exc.__traceback__)
        emit({"kind": "failed", "error_type": type(exc).__name__,
              "error_line": frames[-1].lineno if frames else None})
        sys.exit(1)
