#!/usr/bin/env bash
# conversation.sh — Launch @Lain in conversational mode.
#
# Designed to run continuously and restart automatically when a session exits.
# No gates, no session count limits, no time window constraints.
#
# IMPORTANT: Deletes the Telegram webhook before starting (getUpdates requires it).
# Restores the webhook automatically when this script exits, via trap.
#
# Usage: bash scripts/conversation.sh
#   or:  SESSION_TYPE=conversation bash scripts/conversation.sh

set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/home/andrii/lain/orchestrator_project}"
STATE_DIR="$PROJECT_DIR/state"
LOG_DIR="$PROJECT_DIR/logs"
AGENT_ENV_FILE="$PROJECT_DIR/identity/agent.env"
CONV_DIR="$STATE_DIR/conversation"
PROMPT_FILE="$PROJECT_DIR/prompts/conversation.md"
PERSONA_FILE="$PROJECT_DIR/prompts/persona.txt"
LOCK_FILE="$STATE_DIR/conversation.lock"
WATCHER_PID_FILE="$CONV_DIR/watcher.pid"
AGENT_NAME="${AGENT_NAME:-orchestrator}"
NEXUS_URL="${NEXUS_URL:-http://100.110.36.84:8900}"
NEXUS_PASS_FILE="$PROJECT_DIR/identity/nexus_seed_passwords.txt"

mkdir -p "$STATE_DIR" "$LOG_DIR" "$CONV_DIR"

timestamp() { date '+%Y-%m-%d %H:%M:%S %Z'; }
log_line() { echo "[$(timestamp)] $*" | tee -a "$LOG_DIR/wake.log"; }

# --- Load Telegram credentials ---
# Read token from orchestrator's own identity/agent.env (not Lain's ~/.claude/.env)
TOKEN=$(grep '^TELEGRAM_BOT_TOKEN=' "$AGENT_ENV_FILE" 2>/dev/null | cut -d= -f2 || true)
WEBHOOK_URL_FILE="$CONV_DIR/saved_webhook_url.txt"

# --- Webhook management ---
delete_webhook() {
    if [ -z "$TOKEN" ]; then return; fi
    local resp
    resp=$(curl -s "https://api.telegram.org/bot${TOKEN}/getWebhookInfo")
    local current_url
    current_url=$(echo "$resp" | python3 -c "import sys,json; print(json.load(sys.stdin).get('result',{}).get('url',''))" 2>/dev/null || true)
    if [ -n "$current_url" ]; then
        echo "$current_url" > "$WEBHOOK_URL_FILE"
        curl -s "https://api.telegram.org/bot${TOKEN}/deleteWebhook" > /dev/null
        log_line "CONV: webhook deleted (was: ${current_url}). Saved for restore."
    else
        log_line "CONV: no webhook registered — polling mode already active."
    fi
}

restore_webhook() {
    if [ -z "$TOKEN" ]; then return; fi
    if [ -f "$WEBHOOK_URL_FILE" ]; then
        local url
        url=$(cat "$WEBHOOK_URL_FILE")
        if [ -n "$url" ]; then
            curl -s "https://api.telegram.org/bot${TOKEN}/setWebhook?url=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote('$url',safe=':/'))")" > /dev/null
            log_line "CONV: webhook restored to $url"
        fi
        rm -f "$WEBHOOK_URL_FILE"
    fi
}

# On any exit: restore webhook and remove lock
cleanup() {
    log_line "CONV: exiting — restoring webhook and releasing lock."
    kill_stale_watcher 2>/dev/null || true
    restore_webhook
    rm -f "$LOCK_FILE"
}
trap cleanup EXIT

# --- Lock: prevent concurrent conversation sessions ---
if [ -f "$LOCK_FILE" ]; then
    locked_pid=$(cat "$LOCK_FILE" 2>/dev/null || echo "")
    if [ -n "$locked_pid" ] && kill -0 "$locked_pid" 2>/dev/null; then
        log_line "CONV: already running (PID $locked_pid). Exiting."
        exit 0
    else
        log_line "CONV: stale lock (PID ${locked_pid:-?} dead). Removing."
        rm -f "$LOCK_FILE"
    fi
fi
echo $$ > "$LOCK_FILE"

# --- Delete webhook (enables getUpdates) ---
delete_webhook

export SESSION_TYPE="conversation"
export CURRENT_SESSION_TYPE="conversation"
export TRIGGER_MODE="manual"
# Ensure telegram_watcher.py uses the orchestrator's own token (highest priority source)
export TELEGRAM_BOT_TOKEN_FILE="$PROJECT_DIR/identity/telegram_token.txt"

log_line "CONV: Starting conversation session loop (PID $$)."

# Model selection
MODEL_FILE="$STATE_DIR/session_model.txt"
if [ -f "$MODEL_FILE" ]; then
    MODEL=$(cat "$MODEL_FILE")
else
    MODEL="claude-sonnet-4-6"
fi

refresh_nexus_jwt() {
    if [ ! -f "$NEXUS_PASS_FILE" ]; then return; fi
    local _pass _token
    _pass=$(grep "^# ${AGENT_NAME}" "$NEXUS_PASS_FILE" | grep -o '[^ ]*$' | head -1)
    _token=$(curl -s --max-time 5 -X POST "${NEXUS_URL}/auth/token" \
        -H "Content-Type: application/json" \
        -d "{\"username\":\"${AGENT_NAME}\",\"password\":\"$_pass\"}" \
        | /usr/bin/python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('access_token',''))" \
        2>/dev/null || echo "")
    if [ -n "$_token" ]; then
        echo "$_token" > "$STATE_DIR/nexus_${AGENT_NAME}_token.txt"
        log_line "CONV: Nexus JWT refreshed."
    else
        log_line "CONV: WARNING — Nexus JWT refresh failed (non-fatal)."
    fi
}

kill_stale_watcher() {
    if [ -f "$WATCHER_PID_FILE" ]; then
        local watcher_pid
        watcher_pid=$(cat "$WATCHER_PID_FILE" 2>/dev/null || echo "")
        if [ -n "$watcher_pid" ] && kill -0 "$watcher_pid" 2>/dev/null; then
            # Also kill the bash wrapper spawned by the claude tool — it lingers when
            # python3 is blocked in a getUpdates long-poll and SIGTERM is delayed.
            local wrapper_pid
            wrapper_pid=$(ps -o ppid= -p "$watcher_pid" 2>/dev/null | tr -d '[:space:]' || echo "")
            log_line "CONV: killing stale watcher (PID $watcher_pid) before restart."
            kill "$watcher_pid" 2>/dev/null || true
            if [ -n "$wrapper_pid" ] && [ "$wrapper_pid" != "1" ] && kill -0 "$wrapper_pid" 2>/dev/null; then
                log_line "CONV: killing watcher bash wrapper (PID $wrapper_pid)."
                kill "$wrapper_pid" 2>/dev/null || true
            fi
            # Give it a moment to release the getUpdates connection
            sleep 2
        fi
        rm -f "$WATCHER_PID_FILE"
    fi
}

# Refresh JWT at script startup (before first loop — may be stale if service just started)
refresh_nexus_jwt

# --- Auto-restart loop ---
RESTART_COUNT=0
while true; do
    RESTART_COUNT=$((RESTART_COUNT + 1))
    kill_stale_watcher
    # Clear stale idle-close signal from previous session
    rm -f "$CONV_DIR/reset_signal.txt"
    refresh_nexus_jwt
    log_line "CONV: Session start #$RESTART_COUNT"

    SESSION_OUT="$LOG_DIR/conversation_$(date +%Y-%m-%d)_${RESTART_COUNT}.out"
    SESSION_ERR="$LOG_DIR/conversation_$(date +%Y-%m-%d)_${RESTART_COUNT}.err"

    # Build prompt: conversation.md + optional persona
    SESSION_PROMPT=$(mktemp "$STATE_DIR/conv_prompt.XXXXXX.md")
    if [ -f "$PERSONA_FILE" ]; then
        {
            cat "$PROMPT_FILE"
            echo ""
            echo "---"
            echo ""
            cat "$PERSONA_FILE"
        } > "$SESSION_PROMPT"
    else
        cp "$PROMPT_FILE" "$SESSION_PROMPT"
    fi

    # Start background JWT refresher — token expires in ~1h, refresh every 45min
    ( while true; do sleep 2700; refresh_nexus_jwt; done ) &
    JWT_REFRESH_PID=$!

    # Launch Claude Code in conversation mode
    claude \
        --model "$MODEL" \
        --dangerously-skip-permissions \
        -p "$(cat "$SESSION_PROMPT")" \
        > "$SESSION_OUT" 2> "$SESSION_ERR" || true

    # Stop background refresher now that Claude has exited
    kill "$JWT_REFRESH_PID" 2>/dev/null || true
    wait "$JWT_REFRESH_PID" 2>/dev/null || true

    rm -f "$SESSION_PROMPT"

    EXIT_CODE=$?

    # Read exit reason (written by agent before exiting)
    EXIT_REASON_FILE="$CONV_DIR/exit_reason.txt"
    EXIT_REASON="unknown"
    if [ -f "$EXIT_REASON_FILE" ]; then
        EXIT_REASON=$(cat "$EXIT_REASON_FILE")
        rm -f "$EXIT_REASON_FILE"
    fi

    log_line "CONV: Session #$RESTART_COUNT exited (code=$EXIT_CODE, reason=$EXIT_REASON)."

    if [ "$EXIT_REASON" = "idle_close" ]; then
        log_line "CONV: $EXIT_REASON — not restarting. Service will stop."
        break
    fi

    # Restart on reset/new/context_full/maintenance_close; stop on crashes/unknown.
    # maintenance_close = 4AM context reset, layer stays up (same as agent_project).
    if [ "$EXIT_REASON" = "reset" ] || [ "$EXIT_REASON" = "new" ] || [ "$EXIT_REASON" = "context_full" ] || [ "$EXIT_REASON" = "maintenance_close" ]; then
        log_line "CONV: $EXIT_REASON — restarting in 3s."
        sleep 3
    else
        log_line "CONV: Exit reason '$EXIT_REASON' — not restarting. Service will stop."
        break
    fi
done
