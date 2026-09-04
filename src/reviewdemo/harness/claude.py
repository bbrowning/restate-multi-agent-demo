"""Claude Code adapter.

Patterns here were read off a live 2.1.260 TUI, not guessed. Two observations
worth keeping:

* The ready prompt is "❯" followed by U+00A0 (non-breaking space), not an
  ASCII space. Matching on "❯ " silently never fires.
* After a turn, Claude pre-fills the input box with a *suggested follow-up*
  ("show the diff of that commit"). It is a real editable draft, not grey
  placeholder text, so a bare Enter submits Claude's idea instead of yours.
  objects/pane.py clears with C-u before every send because of this.
"""

from __future__ import annotations

import json
import os
import re

HOOK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks", "on_stop.sh")


class ClaudeHarness:
    name = "claude"

    # The footer shows "esc to interrupt" for exactly as long as a turn runs.
    busy_patterns = (re.compile(r"esc to interrupt"),)

    # U+00A0 after the chevron; allow either space so a future build that
    # switches to a plain space keeps working.
    ready_patterns = (re.compile("❯[  ]"),)

    submit_keys = ("Enter",)

    # Claude can run a Stop hook at turn end, so we never have to guess.
    done_strategy = "hook"

    def settings(self, *, rundir: str, ingress: str) -> dict:
        """Per-run settings injected via --settings.

        The Stop hook is the completion signal: it fires at the end of every
        turn and resolves whichever awakeable the workflow is currently waiting
        on. Paths are passed as argv rather than env so the hook does not
        depend on environment inheritance through the TUI.
        """
        return {
            "hooks": {
                "Stop": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": HOOK,
                                "args": [rundir, ingress],
                                "timeout": 20,
                            }
                        ]
                    }
                ]
            }
        }

    def launch_argv(
        self, *, model: str, effort: str, rundir: str, session_uuid: str, ingress: str
    ) -> list[str]:
        return [
            "claude",
            "--model", model,
            "--effort", effort,
            # The worktree is disposable, so an unattended session that never
            # blocks on a prompt is the right trade.
            "--permission-mode", "bypassPermissions",
            # Fixing the session id makes the transcript path predictable.
            "--session-id", session_uuid,
            "--settings", json.dumps(self.settings(rundir=rundir, ingress=ingress)),
        ]

    def resume_argv(
        self, *, model: str, effort: str, rundir: str, session_uuid: str, ingress: str
    ) -> list[str] | None:
        """Reattach to the pinned session id.

        Because launch_argv pins --session-id, the transcript lands at a known
        path under ~/.claude/projects/<slugified-cwd>/<uuid>.jsonl and survives
        the pane dying. `--resume <uuid>` in the same worktree restores the
        whole conversation -- verified by hand: a resumed session still knew
        the review it had produced and the file it had written.

        --resume replaces --session-id; passing both conflicts.
        """
        return [
            "claude",
            "--resume", session_uuid,
            "--model", model,
            "--effort", effort,
            "--permission-mode", "bypassPermissions",
            "--settings", json.dumps(self.settings(rundir=rundir, ingress=ingress)),
        ]

    def env(self, *, rundir: str, ingress: str) -> dict[str, str]:
        return {"RD_RUNDIR": rundir, "RD_INGRESS": ingress}

    def review_command(self, level: str, target: str = "") -> str:
        """Built-in skill: /code-review [low|medium|high|max] [<pr#>|<branch>|<path>]

        We deliberately pass NO target. The worktree is soft-reset so the
        commit under review is the working tree's pending change, and the
        no-target form reviews exactly that -- the way a human runs it.
        """
        return f"/code-review {level}".strip()


CLAUDE = ClaudeHarness()
