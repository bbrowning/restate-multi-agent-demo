"""Render one status line for run.sh.

    scripts/_status.py <ingress> <run-id> [agent ...]

Kept as a file rather than an inline `python3 -c` because the f-strings need
quotes that do not survive nesting inside a shell double-quoted string.

Every request is time-boxed: a workflow's shared handler BLOCKS until that
workflow has been invoked, so polling the combiner before the reviewers finish
would hang the follower.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

TIMEOUT = 4


def call(ingress: str, path: str) -> dict | None:
    req = urllib.request.Request(f"{ingress}/restate/call/{path}", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read() or "{}")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def main() -> int:
    ingress, run_id, *agents = sys.argv[1:]
    top = call(ingress, f"ReviewWorkflow/{run_id}/status") or {}
    phase = top.get("phase") or "starting"

    parts = [f"{phase:<10}"]
    for agent in agents:
        st = call(ingress, f"AgentSession/{run_id}-{agent}/status")
        if not st:
            parts.append(f"{agent}=--")
            continue
        prog = st.get("progress") or {}
        chars = prog.get("artifact_chars", 0)
        nudges = prog.get("nudges", 0)
        mark = "!" * nudges
        parts.append(f"{agent}={st.get('phase') or '-'}[{chars}c]{mark}")

    print("  ".join(parts))
    return 0 if phase == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
