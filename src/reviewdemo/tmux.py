"""Generic tmux primitives.

Deliberately harness-agnostic: nothing in here knows what an "agent" is. It
spawns a command in a detached session, sends keystrokes, and reads the pane.

The hard-won mechanics (wake-before-send, clear-draft, submit confirmation)
live in objects/pane.py, which composes these primitives.
"""

from __future__ import annotations

import shutil
import subprocess

# tmux truncates very long send-keys arguments; above this we go through a
# paste buffer instead.
MAX_LITERAL = 4096


class TmuxError(RuntimeError):
    pass


def _run(*args: str, check: bool = True, timeout: int = 30) -> subprocess.CompletedProcess:
    if not shutil.which("tmux"):
        raise TmuxError("tmux is not installed")
    proc = subprocess.run(
        ["tmux", "-u", *args], capture_output=True, text=True, timeout=timeout
    )
    if check and proc.returncode != 0:
        raise TmuxError(
            f"tmux {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}"
        )
    return proc


def has_session(name: str) -> bool:
    # a leading '=' forces an exact match rather than a prefix match
    return _run("has-session", "-t", f"={name}", check=False).returncode == 0


def new_session(
    name: str, cwd: str, argv: list[str], env: dict[str, str] | None = None
) -> None:
    """Create a detached session running argv. Idempotent: a no-op if it exists.

    Idempotence matters: Restate replays journalled steps, and a replayed spawn
    must not start a second agent.
    """
    if has_session(name):
        return
    args = ["new-session", "-d", "-s", name, "-c", cwd, "-x", "220", "-y", "50"]
    for key, value in (env or {}).items():
        args += ["-e", f"{key}={value}"]
    args += argv
    _run(*args)
    # Keep the tmux server alive when the last session dies, so a crashed agent
    # leaves a corpse to inspect rather than taking the server down with it.
    _run("set-option", "-g", "exit-empty", "off", check=False)


def capture(name: str, lines: int = 120) -> str:
    """Return the last `lines` of the pane, including scrollback."""
    proc = _run("capture-pane", "-t", name, "-p", "-S", f"-{lines}", check=False)
    return proc.stdout if proc.returncode == 0 else ""


def send_literal(name: str, text: str) -> None:
    """Send text as literal keystrokes, never interpreted as key names."""
    if len(text.encode()) > MAX_LITERAL:
        buf = f"rd{abs(hash(text)) % 10**8}"
        proc = subprocess.run(
            ["tmux", "-u", "load-buffer", "-b", buf, "-"],
            input=text,
            text=True,
            capture_output=True,
            timeout=30,
        )
        if proc.returncode != 0:
            raise TmuxError(f"load-buffer failed: {proc.stderr.strip()}")
        _run("paste-buffer", "-p", "-d", "-b", buf, "-t", name)
        return
    _run("send-keys", "-t", name, "-l", text)


def send_keys(name: str, *keys: str) -> None:
    """Send named keys, e.g. 'Enter', 'Escape', 'C-u'."""
    _run("send-keys", "-t", name, *keys)


def wake_pane(name: str) -> None:
    """Force a SIGWINCH so a detached TUI redraws and starts listening.

    A fully detached TUI can silently drop pasted input. Resizing the pane by
    one row and back is the cheapest reliable wake. Borrowed from gascity,
    which learned it the hard way.
    """
    _run("resize-pane", "-t", name, "-y", "-1", check=False)
    _run("resize-pane", "-t", name, "-y", "+1", check=False)


def pane_dead(name: str) -> bool:
    """True when the session is gone or its pane's process has exited."""
    if not has_session(name):
        return True
    proc = _run("display-message", "-t", name, "-p", "#{pane_dead}", check=False)
    return proc.stdout.strip() == "1"


def kill_session(name: str) -> None:
    _run("kill-session", "-t", name, check=False)


def list_sessions(prefix: str = "") -> list[str]:
    proc = _run("list-sessions", "-F", "#{session_name}", check=False)
    if proc.returncode != 0:
        return []
    return [s for s in proc.stdout.split() if s.startswith(prefix)]
