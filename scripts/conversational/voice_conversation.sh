#!/usr/bin/env bash
# voice_conversation.sh — Home agent voice conversation loop.
#
# Listens for wake word → records → transcribes → queries claude → speaks.
# Repeats indefinitely. SIGTERM/SIGINT: kills active listener, exits cleanly.
#
# Config (identity/agent.env):    ELEVENLABS_API_KEY, HOME_AGENT_VOICE_ID
# Config (state/agent_config.env): WAKE_WORD, WAKE_THRESHOLD
#
# Optional env overrides:
#   STT_MODE         — "local" (default) or "api"
#   HOME_AGENT_MODEL — claude model (default: claude-haiku-4-5-20251001)
#   WAKE_TIMEOUT     — seconds per listen cycle (default: 3600)
#   RECORD_SILENCE   — silence duration before stop in seconds (default: 2.0)
#   RECORD_MAX       — max recording duration in seconds (default: 15.0)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"

PYTHON3="/usr/bin/python3"
CLAUDE_CLI="${CLAUDE_CLI:-claude}"
HOME_AGENT_MODEL="${HOME_AGENT_MODEL:-claude-haiku-4-5-20251001}"

ARGUS_CONTEXT="$PROJECT_DIR/state/argus_context.json"
PERSONA_FILE="$PROJECT_DIR/prompts/persona.txt"
TMP_WAV="/tmp/voice_input_$$.wav"

WAKE_TIMEOUT="${WAKE_TIMEOUT:-3600}"
RECORD_SILENCE="${RECORD_SILENCE:-2.0}"
RECORD_MAX="${RECORD_MAX:-15.0}"

# ── Signal handling ───────────────────────────────────────────────────────────

SHUTDOWN=0
LISTENER_PID=""

cleanup() {
    rm -f "$TMP_WAV"
}
trap cleanup EXIT

handle_sigterm() {
    SHUTDOWN=1
    if [ -n "$LISTENER_PID" ] && kill -0 "$LISTENER_PID" 2>/dev/null; then
        kill "$LISTENER_PID" 2>/dev/null || true
    fi
}
trap handle_sigterm SIGTERM SIGINT

# ── Helpers ───────────────────────────────────────────────────────────────────

log() {
    echo "[voice] $(date +%H:%M:%S) $*" >&2
}

build_prompt() {
    local transcription="$1"
    local prompt=""

    if [ -f "$PERSONA_FILE" ]; then
        prompt="$(cat "$PERSONA_FILE")"$'\n\n'
    fi

    if [ -f "$ARGUS_CONTEXT" ]; then
        prompt="${prompt}Current context: $(cat "$ARGUS_CONTEXT")"$'\n\n'
    fi

    prompt="${prompt}User said: ${transcription}"
    printf '%s' "$prompt"
}

# ── Main loop ─────────────────────────────────────────────────────────────────

log "Starting (model=$HOME_AGENT_MODEL, wake_timeout=${WAKE_TIMEOUT}s)."

while [ "$SHUTDOWN" -eq 0 ]; do

    # Step 1: Listen for wake word
    log "Listening for wake word..."
    wake_exit=0
    "$PYTHON3" "$PROJECT_DIR/tools/conversational/wake_word_listener.py" \
        --max-detections 1 \
        --timeout "$WAKE_TIMEOUT" &
    LISTENER_PID=$!
    wait "$LISTENER_PID" || wake_exit=$?
    LISTENER_PID=""

    [ "$SHUTDOWN" -eq 1 ] && break

    if [ "$wake_exit" -eq 1 ]; then
        log "Wake word timeout — restarting listen cycle."
        continue
    elif [ "$wake_exit" -ne 0 ]; then
        log "Wake word listener error (exit $wake_exit) — retrying in 2s."
        sleep 2
        continue
    fi

    # Step 2: Record until silence
    log "Wake word detected — recording..."
    rm -f "$TMP_WAV"
    record_exit=0
    "$PYTHON3" "$PROJECT_DIR/tools/conversational/home_record.py" \
        --output "$TMP_WAV" \
        --silence-timeout "$RECORD_SILENCE" \
        --max-duration "$RECORD_MAX" || record_exit=$?

    if [ "$record_exit" -eq 2 ]; then
        log "No speech captured — resuming listen."
        continue
    elif [ "$record_exit" -ne 0 ]; then
        log "Recording error (exit $record_exit) — resuming listen."
        continue
    fi

    [ "$SHUTDOWN" -eq 1 ] && break

    # Step 3: Transcribe
    log "Transcribing..."
    transcription=""
    stt_exit=0
    transcription=$("$PYTHON3" "$PROJECT_DIR/tools/conversational/home_stt.py" "$TMP_WAV" 2>/dev/null) \
        || stt_exit=$?
    rm -f "$TMP_WAV"

    if [ "$stt_exit" -ne 0 ] || [ -z "$transcription" ]; then
        log "Transcription empty or failed (exit $stt_exit) — resuming listen."
        continue
    fi

    log "Heard: $transcription"
    [ "$SHUTDOWN" -eq 1 ] && break

    # Step 4: Query claude
    log "Querying claude..."
    prompt=$(build_prompt "$transcription")
    claude_exit=0
    response=$(printf '%s' "$prompt" \
        | "$CLAUDE_CLI" -p --model "$HOME_AGENT_MODEL" 2>/dev/null) \
        || claude_exit=$?

    if [ "$claude_exit" -ne 0 ] || [ -z "$response" ]; then
        log "Claude error (exit $claude_exit) — resuming listen."
        continue
    fi

    # Step 5: Speak response
    log "Speaking..."
    printf '%s' "$response" | bash "$PROJECT_DIR/scripts/conversational/home_tts_play.sh" || {
        log "TTS failed — response was: $response"
    }

done

log "Shutdown complete."
