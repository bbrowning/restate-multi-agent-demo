#!/usr/bin/env bash
# Submit a review run and follow it.
#
#   ./scripts/run.sh <commit-ish> [run-id]
#
# Watch the agents live:   tmux ls  /  tmux attach -t rd-<run-id>-opus
# Watch the journals:      http://localhost:9070
#
# Ctrl-C only stops the follower. The workflow keeps running, and re-running
# this with the same run id will pick the result back up.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INGRESS="${RD_INGRESS:-http://localhost:8080}"
REPO="${RD_REPO:-/pvc/workspace}"
COMMIT="${1:?usage: run.sh <commit-ish> [run-id]}"
RUN_ID="${2:-r$(date +%H%M%S)}"

echo "==> run id : $RUN_ID"
echo "==> commit : $COMMIT  (in $REPO)"

curl -sS --max-time 15 "$INGRESS/restate/send/ReviewWorkflow/$RUN_ID/run" \
     --json "{\"repo\":\"$REPO\",\"commit\":\"$COMMIT\",\"level\":\"low\",\"ingress\":\"$INGRESS\"}"
echo
echo "==> watch:  tmux attach -t rd-$RUN_ID-opus"
echo

while true; do
    if python3 "$ROOT/scripts/_status.py" "$INGRESS" "$RUN_ID" opus sonnet combiner; then
        break
    fi
    sleep 10
done

echo
echo "==> result"
curl -sS --max-time 30 "$INGRESS/restate/attach" \
     --json "{\"target\":\"workflow\",\"workflowName\":\"ReviewWorkflow\",\"workflowKey\":\"$RUN_ID\"}"
echo
echo "==> report: $ROOT/runs/$RUN_ID/report.md"
