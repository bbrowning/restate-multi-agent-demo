#!/usr/bin/env bash
# Claude Code `Stop` hook: fires whenever a turn ends.
#
#   argv: $1 = run directory for THIS agent, $2 = Restate ingress base URL
#   stdin: the Stop payload (session_id, transcript_path, last_assistant_message, ...)
#
# Two jobs:
#   1. append the turn to turns.jsonl  -- durable, replayable, harness-neutral
#   2. resolve the awakeable the workflow is currently blocked on
#
# The awakeable id is read from a file the workflow rewrites before each turn.
# That is how a one-shot awakeable gives us a multi-shot completion signal
# without introducing another Restate concept.
#
# MUST always exit 0: a failing Stop hook would interfere with the session, and
# a missed callback is recoverable (the supervision loop still polls).
set -uo pipefail

RUNDIR="${1:-}"
INGRESS="${2:-http://localhost:8080}"
[ -n "$RUNDIR" ] || exit 0
mkdir -p "$RUNDIR" 2>/dev/null || exit 0

PAYLOAD="$(cat 2>/dev/null || true)"

# Normalise into one compact line; tolerate a non-JSON payload.
LINE="$(printf '%s' "$PAYLOAD" | python3 -c '
import json, sys, time
raw = sys.stdin.read()
try:
    d = json.loads(raw)
except Exception:
    d = {"raw": raw[:2000]}
out = {
    "at": time.time(),
    "session_id": d.get("session_id"),
    "transcript_path": d.get("transcript_path"),
    "stop_hook_active": d.get("stop_hook_active"),
    "last_assistant_message": (d.get("last_assistant_message") or "")[:4000],
}
print(json.dumps(out))
' 2>/dev/null)" || LINE=""

[ -n "$LINE" ] || LINE="{\"at\": $(date +%s), \"note\": \"unparseable payload\"}"
printf '%s\n' "$LINE" >> "$RUNDIR/turns.jsonl" 2>/dev/null || true

# Resolve the awakeable the workflow is waiting on, if any.
AWK_FILE="$RUNDIR/awakeable.id"
if [ -r "$AWK_FILE" ]; then
    AWK_ID="$(tr -d '[:space:]' < "$AWK_FILE" 2>/dev/null || true)"
    if [ -n "$AWK_ID" ]; then
        # Consume the id first: if this turn's callback races a rewrite, we
        # would rather drop a duplicate than resolve the next turn's awakeable.
        rm -f "$AWK_FILE" 2>/dev/null || true
        curl -sS --max-time 10 \
            -X POST "$INGRESS/restate/awakeables/$AWK_ID/resolve" \
            -H 'content-type: application/json' \
            --data "$LINE" >> "$RUNDIR/hook.log" 2>&1 || true
    fi
fi

exit 0
