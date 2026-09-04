#!/usr/bin/env bash
# Remove a run's tmux sessions, git worktrees, and output.
#
#   ./scripts/clean.sh <run-id>     one run
#   ./scripts/clean.sh --all        every run (leaves the app/server alone)
#
# Worktrees must go through `git worktree remove`, or the source repo keeps
# stale admin entries in .git/worktrees.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="${RD_REPO:-/pvc/workspace}"
TARGET="${1:?usage: clean.sh <run-id>|--all}"

if [ "$TARGET" = "--all" ]; then
    pattern="rd-"; runs="$ROOT/runs"/*; wt_glob="$ROOT/runs/"
else
    pattern="rd-$TARGET-"; runs="$ROOT/runs/$TARGET"; wt_glob="$ROOT/runs/$TARGET/"
fi

for s in $(tmux ls 2>/dev/null | grep -o "^${pattern}[^:]*"); do
    case "$s" in rd-app|rd-server) continue ;; esac
    tmux kill-session -t "$s" 2>/dev/null && echo "killed session $s"
done

for w in $(git -C "$REPO" worktree list --porcelain | awk '/^worktree/ {print $2}'); do
    # Scoped to the target run: cleaning r1 must not touch r2's worktrees.
    case "$w" in
        "$wt_glob"*) git -C "$REPO" worktree remove --force "$w" 2>/dev/null \
                       && echo "removed worktree $w" ;;
    esac
done
git -C "$REPO" worktree prune

for d in $runs; do
    [ -e "$d" ] || continue
    rm -rf "$d" && echo "removed $d"
done

echo "done. remaining worktrees:"
git -C "$REPO" worktree list
