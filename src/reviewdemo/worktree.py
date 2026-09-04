"""Git worktree helpers.

Each agent reviews inside its own disposable checkout, so three sessions can
work on the same commit concurrently without fighting over an index.
"""

from __future__ import annotations

import subprocess

# git's canonical empty-tree object, used as the diff base for a root commit
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


class GitError(RuntimeError):
    pass


def _git(repo: str, *args: str, check: bool = True, timeout: int = 120) -> str:
    proc = subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True, timeout=timeout
    )
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def resolve(repo: str, rev: str) -> str:
    """Expand a rev (short sha, branch, HEAD~3) to a full 40-char sha."""
    return _git(repo, "rev-parse", "--verify", f"{rev}^{{commit}}")


def diff_base(repo: str, commit: str) -> str:
    """The ref to diff against: the commit's first parent.

    A root commit has no parent, so fall back to the empty tree — otherwise
    `<sha>^` fails to resolve and the review has nothing to compare against.
    """
    proc = subprocess.run(
        ["git", "-C", repo, "rev-parse", "--verify", f"{commit}^{{commit}}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    parent = subprocess.run(
        ["git", "-C", repo, "rev-parse", "--verify", f"{commit}^1^{{commit}}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise GitError(f"unknown commit {commit}")
    return parent.stdout.strip() if parent.returncode == 0 else EMPTY_TREE


def describe(repo: str, commit: str) -> dict:
    """Commit metadata for the run manifest and the report header."""
    fmt = "%H%n%an%n%aI%n%s"
    out = _git(repo, "show", "-s", f"--format={fmt}", commit)
    sha, author, date, subject = (out.split("\n", 3) + ["", "", "", ""])[:4]
    stat = _git(repo, "show", "--stat", "--oneline", "-s", commit, check=False)
    files = _git(repo, "show", "--name-only", "--format=", commit, check=False)
    return {
        "sha": sha,
        "author": author,
        "date": date,
        "subject": subject,
        "files": [f for f in files.split("\n") if f],
        "stat": stat,
    }


def add(repo: str, path: str, commit: str, soft_reset_to: str | None = None) -> str:
    """Create a detached worktree at `commit`. Idempotent under replay.

    With `soft_reset_to` (normally the commit's parent), HEAD is moved back to
    the base while the files stay as they are. The commit's change then shows
    up as *pending work in the working tree*, which is what a reviewer expects
    to see: `/code-review` with no target reviews exactly this change.

    The alternative -- leaving HEAD at the commit and naming a diff target --
    is fragile: in a clean checkout `git diff` is empty, so a harness that
    falls back to "the current diff" would review nothing at all.
    """
    existing = _git(repo, "worktree", "list", "--porcelain", check=False)
    if f"worktree {path}\n" not in existing + "\n":
        _git(repo, "worktree", "add", "--detach", "--force", path, commit)
    if soft_reset_to:
        head = _git(path, "rev-parse", "HEAD", check=False)
        if head != soft_reset_to:          # idempotent under replay
            _git(path, "reset", "--soft", soft_reset_to)
    return path


def checkpoint(path: str, message: str) -> str | None:
    """Freeze an agent's working tree into an immutable commit; return its sha.

    This is workspace checkpointing on the substrate we already have. The
    workspace *is* a git repo, so a commit is a content-addressed snapshot of
    the whole tree -- cheap (objects are shared with the source repo), and
    crucially a **small, stable pointer** that Restate can journal.

    That pointer/bytes split is the whole trick: Restate stores values, not
    filesystems, and its docs warn against putting blobs in the journal. A sha
    is 40 bytes and, because journal entries replay identically, the next node
    reconstructs *exactly* the same tree on a retry as it did on the first run.

    Returns None when the agent changed nothing (there is no snapshot to take).
    Commits with an explicit machine identity so these never look hand-made.
    """
    _git(path, "add", "-A")
    staged = _git(path, "diff", "--cached", "--name-only", check=False)
    if not staged.strip():
        return None
    _git(
        path,
        "-c", "user.name=reviewdemo",
        "-c", "user.email=reviewdemo@localhost",
        "commit", "--no-verify", "--no-gpg-sign", "-m", message,
    )
    return _git(path, "rev-parse", "HEAD")


def remove(repo: str, path: str) -> None:
    _git(repo, "worktree", "remove", "--force", path, check=False)
