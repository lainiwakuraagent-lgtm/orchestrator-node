#!/usr/bin/env bash
# agent_channel.sh — Launch a per-Nexus-channel agent session.
#
# One persistent claude -p process per Nexus channel/agent relationship.
# Spawned by nexus_watcher.py via: systemctl --user start agent-channel@<channel_id>.service
#
# Each channel gets its own lock and state directory so multiple channels
# can run concurrently without interfering (unlike the single global
# state/conversation.lock that Telegram uses).
#
# Usage: bash scripts/conversational/agent_channel.sh <channel_id>

set -euo pipefail

CHANNEL_ID="${1:?Usage: agent_channel.sh <channel_id>}"

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
STATE_DIR="$PROJECT_DIR/state"
LOG_DIR="$PROJECT_DIR/logs"
CHANNELS_DIR="$STATE_DIR/agent_channels"
CHANNEL_DIR="$CHANNELS_DIR/$CHANNEL_ID"
PROMPT_FILE="$PROJECT_DIR/prompts/agent_channel.md"
PERSONA_FILE="$PROJECT_DIR/prompts/persona.txt"
LOCK_FILE="$CHANNEL_DIR/session.lock"
WATCHER_PID_FILE="$CHANNEL_DIR/watcher.pid"

# Load agent config
AGENT_CONFIG="$STATE_DIR/agent_config.env"
if [ -f "$AGENT_CONFIG" ]; then
    # shellcheck disable=SC1090
    source "$AGENT_CONFIG"
fi
AGENT_NAME="${AGENT_NAME:-lain}"
OWNER_NAME="${OWNER_NAME:-andrii}"

mkdir -p "$STATE_DIR" "$LOG_DIR" "$CHANNEL_DIR"

timestamp() { date '+%Y-%m-%d %H:%M:%S %Z'; }
log_line() { echo "[$(timestamp)] agent_channel[$CHANNEL_ID]: $*" | tee -a "$LOG_DIR/wake.log"; }

# --- Cleanup on any exit ---
cleanup() {
    log_line "exiting — releasing channel lock."
    kill_stale_watcher 2>/dev/null || true
    rm -f "$LOCK_FILE"
}
trap cleanup EXIT

# --- Per-channel lock: prevent concurrent sessions for the same channel ---
if [ -f "$LOCK_FILE" ]; then
    locked_pid=$(cat "$LOCK_FILE" 2>/dev/null || echo "")
    if [ -n "$locked_pid" ] && kill -0 "$locked_pid" 2>/dev/null; then
        log_line "already running (PID $locked_pid). Exiting."
        exit 0
    else
        log_line "stale lock (PID ${locked_pid:-?} dead). Removing."
        rm -f "$LOCK_FILE"
    fi
fi
echo $$ > "$LOCK_FILE"

export SESSION_TYPE="agent_channel"
export CURRENT_SESSION_TYPE="agent_channel"
export TRIGGER_MODE="manual"
export CHANNEL_ID

log_line "Starting channel session loop (PID $$)."

# Model selection
MODEL_FILE="$STATE_DIR/session_model.txt"
if [ -f "$MODEL_FILE" ]; then
    MODEL=$(cat "$MODEL_FILE")
else
    MODEL="claude-sonnet-4-6"
fi

kill_stale_watcher() {
    if [ -f "$WATCHER_PID_FILE" ]; then
        local pid_content watcher_pid wrapper_pid
        pid_content=$(cat "$WATCHER_PID_FILE" 2>/dev/null || echo "")
        watcher_pid="${pid_content%%:*}"
        wrapper_pid="${pid_content##*:}"
        [ "$wrapper_pid" = "$watcher_pid" ] && wrapper_pid=""

        if [ -n "$watcher_pid" ] && kill -0 "$watcher_pid" 2>/dev/null; then
            log_line "killing stale nexus_channel_watcher (PID $watcher_pid)."
            kill "$watcher_pid" 2>/dev/null || true
            sleep 1
        fi
        if [ -n "$wrapper_pid" ] && kill -0 "$wrapper_pid" 2>/dev/null; then
            log_line "killing stale watcher bash wrapper (PID $wrapper_pid)."
            kill "$wrapper_pid" 2>/dev/null || true
        fi
        rm -f "$WATCHER_PID_FILE"
    fi
    # Kill any orphaned nexus_channel_watcher.py for this specific channel
    while IFS= read -r stray_pid; do
        stray_comm=$(cat "/proc/$stray_pid/comm" 2>/dev/null || echo "")
        if [[ "$stray_comm" == python* ]]; then
            log_line "pkill fallback — killing stray watcher python PID $stray_pid."
            kill "$stray_pid" 2>/dev/null || true
        fi
    done < <(pgrep -f "nexus_channel_watcher.*${CHANNEL_ID}" 2>/dev/null || true)
}

# One-time startup sweep: kill any watcher orphans from prior runs.
log_line "startup — sweeping for pre-session watcher orphans."
_sw_killed=0
while IFS= read -r _sw_pid; do
    _sw_comm=$(cat "/proc/$_sw_pid/comm" 2>/dev/null || echo "")
    if [[ "$_sw_comm" == python* ]]; then
        log_line "startup sweep — killing stale watcher PID $_sw_pid."
        kill "$_sw_pid" 2>/dev/null || true
        _sw_killed=1
    fi
done < <(pgrep -f "nexus_channel_watcher.*${CHANNEL_ID}" 2>/dev/null || true)
[ "$_sw_killed" = "1" ] && sleep 1

# --- Auto-restart loop ---
RESTART_COUNT=0
while true; do
    RESTART_COUNT=$((RESTART_COUNT + 1))
    kill_stale_watcher
    log_line "Session start #$RESTART_COUNT"

    SESSION_OUT="$LOG_DIR/channel_${CHANNEL_ID}_$(date +%Y-%m-%d)_${RESTART_COUNT}.out"
    SESSION_ERR="$LOG_DIR/channel_${CHANNEL_ID}_$(date +%Y-%m-%d)_${RESTART_COUNT}.err"

    # Build prompt: agent_channel.md + optional persona, substituting tokens
    SESSION_PROMPT=$(mktemp "$STATE_DIR/channel_prompt.XXXXXX.md")
    if [ -f "$PERSONA_FILE" ]; then
        {
            sed \
                -e "s/\${AGENT_NAME}/${AGENT_NAME}/g" \
                -e "s/\${OWNER_NAME}/${OWNER_NAME}/g" \
                -e "s/\${CHANNEL_ID}/${CHANNEL_ID}/g" \
                "$PROMPT_FILE"
            echo ""
            echo "---"
            echo ""
            cat "$PERSONA_FILE"
        } > "$SESSION_PROMPT"
    else
        sed \
            -e "s/\${AGENT_NAME}/${AGENT_NAME}/g" \
            -e "s/\${OWNER_NAME}/${OWNER_NAME}/g" \
            -e "s/\${CHANNEL_ID}/${CHANNEL_ID}/g" \
            "$PROMPT_FILE" > "$SESSION_PROMPT"
    fi

    EXIT_CODE=0
    set +e
    claude \
        --model "$MODEL" \
        --dangerously-skip-permissions \
        -p "$(cat "$SESSION_PROMPT")" \
        > "$SESSION_OUT" 2> "$SESSION_ERR"
    EXIT_CODE=$?
    set -e
    rm -f "$SESSION_PROMPT"

    # Read exit reason (written by agent to per-channel exit_reason.txt)
    EXIT_REASON_FILE="$CHANNEL_DIR/exit_reason.txt"
    EXIT_REASON="unknown"
    if [ -f "$EXIT_REASON_FILE" ]; then
        EXIT_REASON=$(cat "$EXIT_REASON_FILE")
        rm -f "$EXIT_REASON_FILE"
    fi

    log_line "Session #$RESTART_COUNT exited (code=$EXIT_CODE, reason=$EXIT_REASON)."

    # Write conversational analytics record for this channel session.
    /usr/bin/python3 "$PROJECT_DIR/tools/conversational/conversational_analytics_write.py" \
        --channel "$CHANNEL_ID" \
        --exit-reason "$EXIT_REASON" \
        2>/dev/null || true

    case "$EXIT_REASON" in
        context_full)
            # Context window exhausted — restart with checkpoint for continuity
            log_line "Context full — restarting with checkpoint."
            sleep 3
            ;;
        closed_by_agent)
            # Agent invoked /close-comms-session and decided the exchange is done
            log_line "Session closed cleanly by agent. Stopping."
            exit 0
            ;;
        *)
            if [ "$EXIT_CODE" -ne 0 ]; then
                log_line "Crashed (code=$EXIT_CODE) — restarting in 30s."
                sleep 30
            else
                # Clean zero exit without a recognized reason — stop the service.
                # nexus_watcher.py will re-spawn if new messages arrive.
                log_line "Clean exit (reason=${EXIT_REASON}) — stopping service."
                exit 0
            fi
            ;;
    esac
done
