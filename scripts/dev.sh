#!/usr/bin/env bash
# Start/stop the pieces.
#
#   ./scripts/dev.sh up | down | restart | status | logs
#
# The SDK endpoint runs inside a tmux session ("rd-app") rather than as a
# background process. tmux is already this demo's process substrate, it
# survives the launching shell exiting, and `tmux attach -t rd-app` gives you
# the app's log live.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOGS="$ROOT/.dev"; mkdir -p "$LOGS"
APP_SESSION=rd-app
SRV_SESSION=rd-server

up_server() {
    if curl -sf --max-time 2 http://localhost:9070/health >/dev/null 2>&1; then
        echo "server: already up"; return 0
    fi
    tmux new-session -d -s "$SRV_SESSION" -c "$ROOT" './bin/restate-server 2>&1 | tee .dev/server.log'
    for _ in $(seq 1 30); do
        curl -sf --max-time 2 http://localhost:9070/health >/dev/null 2>&1 && { echo "server: up"; return 0; }
        sleep 1
    done
    echo "server failed; tmux attach -t $SRV_SESSION" >&2; return 1
}

up_app() {
    tmux kill-session -t "$APP_SESSION" 2>/dev/null
    sleep 1
    tmux new-session -d -s "$APP_SESSION" -c "$ROOT" \
        'uv run python -m reviewdemo.app 2>&1 | tee .dev/app.log'
    for _ in $(seq 1 40); do
        tmux capture-pane -t "$APP_SESSION" -p 2>/dev/null | grep -q "Running on" && {
            echo "app: up on :9080"; return 0; }
        tmux capture-pane -t "$APP_SESSION" -p 2>/dev/null | grep -qE "Traceback|Address already in use" && {
            echo "app failed:" >&2; tmux capture-pane -t "$APP_SESSION" -p | tail -20 >&2; return 1; }
        sleep 1
    done
    echo "app did not start; tmux attach -t $APP_SESSION" >&2; return 1
}

register() {
    "$ROOT/bin/restate" deployments register http://localhost:9080 --force --yes 2>&1 \
        | grep -E '^\s+(TmuxPane|AgentSession|ReviewWorkflow)\s+[0-9]+' || true
}

case "${1:-up}" in
    up)      up_server && up_app && register ;;
    restart) up_app && register ;;
    down)
        tmux kill-session -t "$APP_SESSION" 2>/dev/null && echo "app: stopped"
        tmux kill-session -t "$SRV_SESSION" 2>/dev/null && echo "server: stopped"
        ;;
    logs)    tail -40 "$LOGS/app.log" ;;
    status)
        curl -sf --max-time 2 http://localhost:9070/health >/dev/null 2>&1 \
            && echo "server: up  (UI http://localhost:9070)" || echo "server: down"
        tmux has-session -t "$APP_SESSION" 2>/dev/null \
            && echo "app:    up  (tmux attach -t $APP_SESSION)" || echo "app:    down"
        echo "agent sessions:"; tmux ls 2>/dev/null | grep '^rd-' | grep -v "$APP_SESSION\|$SRV_SESSION" || echo "  (none)"
        ;;
    *) echo "usage: dev.sh {up|down|restart|status|logs}" >&2; exit 2 ;;
esac
