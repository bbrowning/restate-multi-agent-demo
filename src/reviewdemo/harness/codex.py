"""Codex adapter -- UNTESTED.

There is no codex binary on this machine, so none of this has ever run. It
exists to prove the seam is real: the workflow and pane layers import nothing
claude-specific, and a second harness is genuinely just this file.

Treat every constant below as a guess to be replaced by reading a live TUI the
way harness/claude.py's patterns were derived. Specifically unverified:
  * the busy/ready patterns
  * whether Enter submits or inserts a newline
  * the review command syntax
  * whether codex has any turn-end hook (assumed not, hence "quiescence")
"""

from __future__ import annotations

import re


class CodexHarness:
    name = "codex"

    busy_patterns = (
        re.compile(r"esc to interrupt", re.I),
        re.compile(r"\bthinking\b", re.I),
        re.compile(r"\bworking\b", re.I),
    )
    ready_patterns = (re.compile(r"^[>❯▌]\s", re.M),)
    submit_keys = ("Enter",)

    # No known turn-end hook: fall back to watching the pane go quiet.
    done_strategy = "quiescence"

    def launch_argv(
        self, *, model: str, effort: str, rundir: str, session_uuid: str, ingress: str
    ) -> list[str]:
        return ["codex", "--model", model]

    def env(self, *, rundir: str, ingress: str) -> dict[str, str]:
        return {"RD_RUNDIR": rundir, "RD_INGRESS": ingress}

    def review_command(self, level: str, target: str = "") -> str:
        # Same contract: review the working tree's pending change.
        return "Review the uncommitted changes in this working tree and report bugs and cleanups."


CODEX = CodexHarness()
