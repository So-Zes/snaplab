"""Plain git operations that SnapLab runs itself (outside any sandbox; no model output is executed here).

- Every project folder is a git repository (branch "main") with BRIEF.md, AGENTS.md and a .gitignore.
- Every session works in its own worktree <project>/.worktrees/<session> on branch snaplab/<session>,
  so the project's main folder stays clean while agents work.
- After each turn SnapLab commits the worktree's changes (the sandbox keeps .git read-only for the agent)
  and measures the change against the session's starting commit (git diff --stat / --numstat).
- "Merge" brings a session's branch into main with a merge commit; on conflicts the merge is aborted.

Commits are made as "SnapLab <snaplab@localhost>" (set in each repository), never with the user's identity.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

AUTHOR_NAME = "SnapLab"
AUTHOR_EMAIL = "snaplab@localhost"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
GITIGNORE = ".worktrees/\n.snaplab/\nSTATE.md\n__pycache__/\n*.pyc\n"


def available() -> bool:
    return bool(shutil.which("git"))


def run(cwd: Path, *args: str, timeout: float | None = None) -> subprocess.CompletedProcess:
    """No time limit by default (2026-11-17). It was 60 s: creating a worktree of a 27,600-file project took longer
    while the PC was busy, and the new session failed."""
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    # Never climb to an enclosing repository: project folders live under data/, inside SnapLab's own checkout, and
    # a folder without its own .git would otherwise commit SnapLab itself ("git add -A" stages the whole tree).
    env["GIT_CEILING_DIRECTORIES"] = str(Path(cwd).resolve().parent)
    # core.quotepath=false: file names such as from_chat/買い物.csv come back as they are, not as octal escapes.
    return subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, env=env, creationflags=NO_WINDOW)


def is_repo(path: Path) -> bool:
    return (Path(path) / ".git").exists()


def _enc(name: str) -> str:
    """Markdown files get a UTF-8 BOM so Windows PowerShell 5.1 (what Codex runs) does not read them as Shift-JIS
    (QA round 2, M-4); other files stay plain UTF-8."""
    return "utf-8-sig" if name.lower().endswith(".md") else "utf-8"


def init_repo(path: Path, files: dict[str, str]) -> str | None:
    """git init with the given starting files and one commit. Returns the commit id (or None without git)."""
    path = Path(path)
    if not available():
        for name, text in files.items():
            (path / name).write_text(text, encoding=_enc(name))
        return None
    run(path, "init", "-q", "-b", "main")
    run(path, "config", "user.name", AUTHOR_NAME)
    run(path, "config", "user.email", AUTHOR_EMAIL)
    run(path, "config", "core.autocrlf", "false")
    run(path, "config", "commit.gpgsign", "false")
    files = {".gitignore": GITIGNORE, **files}
    for name, text in files.items():
        (path / name).write_text(text, encoding=_enc(name))
    run(path, "add", "-A")
    run(path, "commit", "-q", "-m", "Start the project (SnapLab)")
    return head(path)


def head(path: Path) -> str | None:
    p = run(path, "rev-parse", "HEAD")
    return p.stdout.strip() if p.returncode == 0 else None


def git_dir(path: Path) -> Path:
    return Path(path) / ".git"


def add_worktree(project: Path, sid: str, from_ref: str | None = None) -> tuple[Path, str, str]:
    """Create <project>/.worktrees/<sid> on a new branch from main, or from `from_ref` (another session's branch,
    so a fresh agent starts from that session's files). Returns (folder, branch, base commit)."""
    project = Path(project)
    branch = f"snaplab/{sid}"
    folder = project / ".worktrees" / sid
    if from_ref:
        p = run(project, "rev-parse", "--verify", "-q", from_ref + "^{commit}")
        base = p.stdout.strip() if p.returncode == 0 else None
        if not base:
            raise RuntimeError(f"no commit for {from_ref}")
    else:
        base = head(project)
    if not base:
        raise RuntimeError("the project repository has no commit yet")
    p = run(project, "worktree", "add", "-q", "-b", branch, str(folder), base)
    if p.returncode != 0:
        raise RuntimeError(f"git worktree failed: {p.stderr.strip()[:300]}")
    return folder, branch, base


def clean_blobs(folder: Path) -> dict[str, tuple[str, int]]:
    """Tracked regular files whose working copy git considers unchanged (by its own stat check):
    path -> (blob id, size). Empty when the folder is not a repository or git fails."""
    listed = run(folder, "ls-files", "-s", "-z")
    if listed.returncode:
        return {}
    blobs = {}
    for record in listed.stdout.split("\0"):
        meta, _, path = record.partition("\t")
        parts = meta.split()
        if path and len(parts) == 3 and parts[2] == "0" and parts[0] in ("100644", "100755"):
            blobs[path] = parts[1]
    changed = run(folder, "diff-files", "--name-only", "-z")
    if changed.returncode:
        return {}
    for path in changed.stdout.split("\0"):
        blobs.pop(path, None)
    if not blobs:
        return {}
    ids = sorted(set(blobs.values()))
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE")}
    env["GIT_CEILING_DIRECTORIES"] = str(Path(folder).resolve().parent)
    sized = subprocess.run(["git", "cat-file", "--batch-check=%(objectname) %(objectsize)"], cwd=str(folder),
                           input="\n".join(ids) + "\n", capture_output=True, text=True, encoding="utf-8",
                           env=env, creationflags=NO_WINDOW)
    if sized.returncode:
        return {}
    sizes = {}
    for line in sized.stdout.splitlines():
        name, _, size = line.partition(" ")
        if size.isdigit():
            sizes[name] = int(size)
    return {path: (blob, sizes[blob]) for path, blob in blobs.items() if blob in sizes}


def common_dir(folder: Path) -> Path | None:
    """The repository's shared git directory (the same for every worktree of a project)."""
    p = run(folder, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(p.stdout.strip()) if p.returncode == 0 and p.stdout.strip() else None


def commit_all(folder: Path, message: str) -> str | None:
    run(folder, "add", "-A")
    p = run(folder, "diff", "--cached", "--quiet")
    if p.returncode == 0:
        return None          # nothing changed
    c = run(folder, "commit", "-q", "-m", message)
    if c.returncode != 0:
        return None
    return head(folder)


def diff_summary(folder: Path, base: str) -> dict:
    """Changes from the session's starting commit to now: files with A/M/D/R, insertions, deletions, --stat text."""
    names = run(folder, "diff", "--name-status", "-M", base, "HEAD")
    changes = []
    for line in names.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        code = parts[0][:1]
        kind = {"A": "added", "M": "modified", "D": "deleted", "R": "renamed"}.get(code, "modified")
        changes.append({"path": parts[-1], "change": kind})
    ins = dels = 0
    for line in run(folder, "diff", "--numstat", base, "HEAD").stdout.splitlines():
        a, b, *_ = (line.split("\t") + ["", ""])[:3]
        ins += int(a) if a.isdigit() else 0
        dels += int(b) if b.isdigit() else 0
    stat = run(folder, "diff", "--stat", base, "HEAD").stdout.strip()
    return {"files": len(changes), "insertions": ins, "deletions": dels, "changes": changes, "stat": stat[-2000:]}


def patch(folder: Path, since: str, max_bytes: int = 2_000_000) -> str:
    """The full diff from a commit to HEAD (text files; binary files are named only)."""
    p = run(folder, "diff", "--no-color", "-M", since, "HEAD")
    out = p.stdout if p.returncode == 0 else ""
    if len(out.encode("utf-8", "replace")) > max_bytes:
        out = out[:max_bytes] + "\n… (cut at the size limit)\n"
    return out


def merge(project: Path, branch: str, message: str) -> tuple[bool, str]:
    project = Path(project)
    if run(project, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        return False, "the main folder has uncommitted changes"
    p = run(project, "merge", "--no-ff", "-m", message, branch)
    if p.returncode != 0:
        run(project, "merge", "--abort")
        return False, (p.stdout + p.stderr).strip()[-300:] or "merge failed"
    return True, head(project) or ""


def exclude(project: Path, pattern: str) -> None:
    """Add a pattern to .git/info/exclude (shared by all worktrees)."""
    f = git_dir(project) / "info" / "exclude"
    f.parent.mkdir(parents=True, exist_ok=True)
    cur = f.read_text(encoding="utf-8") if f.exists() else ""
    if pattern not in cur.splitlines():
        f.write_text(cur + ("" if cur.endswith("\n") or not cur else "\n") + pattern + "\n", encoding="utf-8")
