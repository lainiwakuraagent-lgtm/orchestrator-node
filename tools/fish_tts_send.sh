#!/usr/bin/env bash
# fish_tts_send.sh — Convert text to Fish Audio voice and send as Telegram voice note
#
# Usage: echo "text to speak" | bash tools/fish_tts_send.sh
#    OR: printf '%s' "text" | bash tools/fish_tts_send.sh
#
# Requires:
#   - FISH_AUDIO_API_KEY in ~/.claude/.env
#   - FISH_VOICE_ID in ~/.claude/.env (or state/voice_config.json)
#   - TELEGRAM_BOT_TOKEN in ~/.claude/.env
#   - TELEGRAM_CHAT_ID: owner's chat_id (from TELEGRAM_ALLOWED_USERS in .env)
#
# State files:
#   state/voice_mode.txt — "on" or "off" (voice mode toggle)
#   state/voice_config.json — { "voice_id": "...", "format": "mp3" }
#
# Exit codes:
#   0 — voice note sent successfully
#   1 — error (API failure, empty text, etc.)
#   2 — voice mode is off (caller can skip silently)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
# Prefer agent-specific credentials (identity/agent.env), fall back to ~/.claude/.env
AGENT_ENV="$PROJECT_DIR/identity/agent.env"
ENV_FILE="${AGENT_ENV:-$HOME/.claude/.env}"
if [ ! -f "$ENV_FILE" ]; then ENV_FILE="$HOME/.claude/.env"; fi
VOICE_MODE_FILE="$PROJECT_DIR/state/voice_mode.txt"
VOICE_CONFIG_FILE="$PROJECT_DIR/state/voice_config.json"

# ── Check voice mode ──────────────────────────────────────────────────────

VOICE_MODE=$(cat "$VOICE_MODE_FILE" 2>/dev/null | tr -d '[:space:]' || echo "off")
if [ "$VOICE_MODE" != "on" ]; then
    exit 2  # voice mode off — caller treats this as "skip silently"
fi

# ── Load config ───────────────────────────────────────────────────────────

load_env_var() {
    grep "^${1}=" "$ENV_FILE" 2>/dev/null | cut -d= -f2 | head -1 | tr -d '[:space:]'
}

FISH_API_KEY=$(load_env_var "FISH_AUDIO_API_KEY")
BOT_TOKEN=$(load_env_var "TELEGRAM_BOT_TOKEN")
# Support both TELEGRAM_CHAT_ID (agent.env) and TELEGRAM_ALLOWED_USERS (~/.claude/.env)
CHAT_ID="${TELEGRAM_CHAT_ID:-$(load_env_var "TELEGRAM_CHAT_ID" || true)}"
if [ -z "$CHAT_ID" ]; then CHAT_ID=$(load_env_var "TELEGRAM_ALLOWED_USERS" || true); fi

if [ -z "$FISH_API_KEY" ]; then
    echo "ERROR: FISH_AUDIO_API_KEY not set in $ENV_FILE" >&2
    exit 1
fi

if [ -z "$BOT_TOKEN" ]; then
    echo "ERROR: TELEGRAM_BOT_TOKEN not set in $ENV_FILE" >&2
    exit 1
fi

if [ -z "$CHAT_ID" ]; then
    echo "ERROR: CHAT_ID not resolved (set TELEGRAM_CHAT_ID or TELEGRAM_ALLOWED_USERS)" >&2
    exit 1
fi

# Voice ID from config file or env
VOICE_ID=""
if [ -f "$VOICE_CONFIG_FILE" ]; then
    VOICE_ID=$(/usr/bin/python3 -c "
import json
d = json.load(open('$VOICE_CONFIG_FILE'))
print(d.get('voice_id', ''))
" 2>/dev/null || echo "")
fi
VOICE_ID="${FISH_VOICE_ID:-${VOICE_ID:-}}"

if [ -z "$VOICE_ID" ]; then
    echo "ERROR: FISH_VOICE_ID not set. Set in $ENV_FILE or $VOICE_CONFIG_FILE" >&2
    exit 1
fi

FORMAT="${FISH_AUDIO_FORMAT:-mp3}"

# ── Read text from stdin ──────────────────────────────────────────────────

TEXT=$(cat)

if [ -z "$TEXT" ]; then
    echo "ERROR: empty text — pipe content via stdin" >&2
    exit 1
fi

# ── Call Fish Audio TTS API ───────────────────────────────────────────────

AUDIO_FILE="/tmp/fish_tts_$$.${FORMAT}"

# Fish Audio v1 TTS endpoint
BODY=$(/usr/bin/python3 -c "
import json, sys
print(json.dumps({
    'text': '''$TEXT''',
    'reference_id': '$VOICE_ID',
    'format': '$FORMAT',
    'latency': 'normal',
}))
" 2>/dev/null)

HTTP_STATUS=$(curl -s -o "$AUDIO_FILE" -w "%{http_code}" \
    -X POST \
    -H "Authorization: Bearer $FISH_API_KEY" \
    -H "Content-Type: application/json" \
    "https://api.fish.audio/v1/tts" \
    -d "$BODY")

if [ "$HTTP_STATUS" != "200" ]; then
    echo "ERROR: Fish Audio API returned status $HTTP_STATUS" >&2
    cat "$AUDIO_FILE" >&2 2>/dev/null || true
    rm -f "$AUDIO_FILE"
    exit 1
fi

# ── Send as Telegram voice note ───────────────────────────────────────────

RESPONSE=$(curl -s -X POST \
    "https://api.telegram.org/bot${BOT_TOKEN}/sendVoice" \
    -F "chat_id=${CHAT_ID}" \
    -F "voice=@${AUDIO_FILE};type=audio/mpeg")

rm -f "$AUDIO_FILE"

SUCCESS=$(/usr/bin/python3 -c "
import json, sys
r = json.load(sys.stdin)
print('ok' if r.get('ok') else 'fail')
" <<< "$RESPONSE" 2>/dev/null || echo "fail")

if [ "$SUCCESS" = "ok" ]; then
    MSG_ID=$(/usr/bin/python3 -c "
import json, sys
r = json.load(sys.stdin)
print(r['result']['message_id'])
" <<< "$RESPONSE" 2>/dev/null || echo "?")
    echo "fish_tts_send: voice note sent (message_id=$MSG_ID)"
else
    echo "ERROR: Telegram sendVoice failed: $RESPONSE" >&2
    exit 1
fi
