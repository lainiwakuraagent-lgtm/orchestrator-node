#!/usr/bin/env bash
# check_conv_status.sh — Report conversational layer health.
#
# Outputs JSON to stdout:
#   {
#     "running": true/false,
#     "pid": <int or null>,
#     "uptime_seconds": <int or null>,
#     "context_pct": <int>,
#     "messages_sent": <int>,
#     "messages_received": <int>,
#     "session_start": <unix_ts or null>,
#     "watcher_running": true/false
#   }
#
# Usage:
#   bash tools/check_conv_status.sh
#   bash tools/check_conv_status.sh --text    # human-readable output

set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SERVICE_NAME="conversation@$(basename "$PROJECT_DIR").service"
LOCK_FILE="$PROJECT_DIR/state/conversation.lock"
BUDGET_FILE="$PROJECT_DIR/state/conversation/context_budget.json"
WATCHER_PID_FILE="$PROJECT_DIR/state/conversation/watcher.pid"
TEXT_MODE=0
[ "${1:-}" = "--text" ] && TEXT_MODE=1

# --- Check conversation.sh PID ---
running=false
pid="null"
uptime_seconds="null"

if [ -f "$LOCK_FILE" ]; then
    lock_pid=$(cat "$LOCK_FILE" 2>/dev/null || echo "")
    if [ -n "$lock_pid" ] && kill -0 "$lock_pid" 2>/dev/null; then
        running=true
        pid="$lock_pid"
        # Compute uptime
        start_time=$(ps -o lstart= -p "$lock_pid" 2>/dev/null | head -1 || echo "")
        if [ -n "$start_time" ]; then
            start_epoch=$(date -d "$start_time" +%s 2>/dev/null || echo "")
            if [ -n "$start_epoch" ]; then
                now_epoch=$(date +%s)
                uptime_seconds=$((now_epoch - start_epoch))
            fi
        fi
    fi
fi

# --- Check watcher ---
watcher_running=false
if [ -f "$WATCHER_PID_FILE" ]; then
    wpid=$(cat "$WATCHER_PID_FILE" 2>/dev/null || echo "")
    if [ -n "$wpid" ] && kill -0 "$wpid" 2>/dev/null; then
        watcher_running=true
    fi
fi

# --- Read context budget ---
context_pct=0
messages_sent=0
messages_received=0
session_start="null"

if [ -f "$BUDGET_FILE" ]; then
    context_pct=$(/usr/bin/python3 -c "
import json, sys
d=json.load(open('$BUDGET_FILE'))
print(d.get('estimated_context_pct', 0))
" 2>/dev/null || echo "0")
    messages_sent=$(/usr/bin/python3 -c "
import json
d=json.load(open('$BUDGET_FILE'))
print(d.get('messages_sent', 0))
" 2>/dev/null || echo "0")
    messages_received=$(/usr/bin/python3 -c "
import json
d=json.load(open('$BUDGET_FILE'))
print(d.get('messages_received', 0))
" 2>/dev/null || echo "0")
    session_start=$(/usr/bin/python3 -c "
import json
d=json.load(open('$BUDGET_FILE'))
v=d.get('session_start')
print(v if v else 'null')
" 2>/dev/null || echo "null")
fi

if [ "$TEXT_MODE" -eq 1 ]; then
    if [ "$running" = "true" ]; then
        echo "$SERVICE_NAME: RUNNING (PID $pid, uptime ${uptime_seconds}s)"
    else
        echo "$SERVICE_NAME: NOT RUNNING"
    fi
    echo "watcher: $watcher_running"
    echo "context: ${context_pct}%"
    echo "messages: sent=$messages_sent received=$messages_received"
else
    running_bool=$([ "$running" = "true" ] && echo "True" || echo "False")
    watcher_bool=$([ "$watcher_running" = "true" ] && echo "True" || echo "False")
    /usr/bin/python3 -c "
import json
print(json.dumps({
    'running': ${running_bool},
    'pid': ${pid},
    'uptime_seconds': ${uptime_seconds},
    'context_pct': ${context_pct},
    'messages_sent': ${messages_sent},
    'messages_received': ${messages_received},
    'session_start': ${session_start},
    'watcher_running': ${watcher_bool},
}, indent=2))
"
fi
