"""The harness seam.

Everything above this line (workflows, the pane object) is written against
`Harness` and never mentions a specific agent CLI. Adding codex / pi / opencode
is one new module plus a registry entry.

Nothing here imports restate: adapters stay plain, testable Python.
"""

from __future__ import annotations

import re
from typing import Protocol, runtime_checkable


@runtime_checkable
class Harness(Protocol):
    """One agent CLI, described well enough to drive it blind through tmux."""

    name: str

    #: Footer/status text meaning "a turn is in flight". Checked FIRST: a busy
    #: pane is never idle, whatever else is on screen.
    busy_patterns: tuple[re.Pattern[str], ...]

    #: The input prompt, meaning "accepting keystrokes".
    ready_patterns: tuple[re.Pattern[str], ...]

    #: Keys that submit the composed prompt.
    submit_keys: tuple[str, ...]

    #: "hook"       -> the CLI can call out when a turn ends (precise)
    #: "quiescence" -> we must infer turn end by watching the pane (fragile)
    done_strategy: str

    def launch_argv(
        self, *, model: str, effort: str, rundir: str, session_uuid: str, ingress: str
    ) -> list[str]:
        """argv for a fresh interactive session in the agent's worktree."""
        ...

    def resume_argv(
        self, *, model: str, effort: str, rundir: str, session_uuid: str, ingress: str
    ) -> list[str] | None:
        """argv that reattaches to a previous conversation, or None.

        This is what makes an agent's *memory* durable rather than just the
        orchestration. A pane can die (crash, reboot, someone closes tmux)
        while the worktree and the harness's own transcript survive on disk;
        with this we respawn into the same worktree and the agent still knows
        what it was doing.

        Return None if the harness cannot resume -- the workflow will then
        treat a dead pane as terminal instead of pretending it recovered.
        """
        ...

    def env(self, *, rundir: str, ingress: str) -> dict[str, str]:
        """Extra environment injected into the tmux session."""
        ...

    def review_command(self, level: str, target: str) -> str:
        """Ask for a code review of `target` at the given effort level."""
        ...


def classify(harness: Harness, pane: str) -> str:
    """Map raw pane text to 'busy' | 'ready' | 'unknown'.

    Order matters. A busy pane can still show the prompt box, so the busy test
    must win. This is liveness only -- never proof that work is *finished*.
    """
    for pat in harness.busy_patterns:
        if pat.search(pane):
            return "busy"
    for pat in harness.ready_patterns:
        if pat.search(pane):
            return "ready"
    return "unknown"


def prompt_empty(harness: Harness, pane: str) -> bool | None:
    """Is the input box currently empty?

    Returns None when we cannot tell. Used to confirm a submit actually landed:
    after typing, the box holds our text; after the submit key, it clears.

    This matters because the busy indicator alone is not enough. Claude's
    /code-review dispatches a background sub-agent and returns to idle almost
    immediately, so "not busy" would wrongly read as "my keystrokes never
    arrived" and trigger a duplicate send.

    We look at the LAST prompt match in the pane -- earlier ones are previous
    turns scrolled up into the transcript.
    """
    tail: str | None = None
    for line in pane.split("\n"):
        for pat in harness.ready_patterns:
            match = pat.search(line)
            if match:
                tail = line[match.end():]
    if tail is None:
        return None
    return tail.replace("\xa0", " ").strip() == ""


def get(name: str) -> Harness:
    from . import claude, codex

    registry: dict[str, Harness] = {
        claude.CLAUDE.name: claude.CLAUDE,
        codex.CODEX.name: codex.CODEX,
    }
    if name not in registry:
        raise KeyError(f"unknown harness {name!r}; have {sorted(registry)}")
    return registry[name]
