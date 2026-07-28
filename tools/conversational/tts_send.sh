#!/usr/bin/env bash
# tts_send.sh — Convert text to ElevenLabs voice and send as Telegram voice note
#
# Usage: echo "text to speak" | bash tts_send.sh
#    OR: bash tts_send.sh <<'EOF'
#        multi-line text
#        EOF
#
# Requires:
#   - ELEVENLABS_API_KEY in ~/.config/PAI/.env
#   - TELEGRAM_BOT_TOKEN in ~/.claude/.env
#   - LAIN_VOICE_ID set in identity/credentials.md or as env var
#   - TELEGRAM_CHAT_ID: owner's chat_id (falls back to TELEGRAM_ALLOWED_USERS in ~/.claude/.env)
#
# Note: voice_id must be provided via LAIN_VOICE_ID env var or set in this script
# once selected by owner. Until then, set LAIN_VOICE_ID before calling.

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────

ELEVENLABS_KEY=$(grep 'ELEVENLABS_API_KEY' ~/.config/PAI/.env | cut -d= -f2)
BOT_TOKEN=$(grep 'TELEGRAM_BOT_TOKEN' ~/.claude/.env | cut -d= -f2)
ALLOWED_USERS=$(grep 'TELEGRAM_ALLOWED_USERS' ~/.claude/.env 2>/dev/null | cut -d= -f2)
CHAT_ID="${TELEGRAM_CHAT_ID:-$ALLOWED_USERS}"

if [[ -z "$CHAT_ID" ]]; then
    echo "ERROR: CHAT_ID not resolved (set TELEGRAM_CHAT_ID or TELEGRAM_ALLOWED_USERS in ~/.claude/.env)" >&2
    exit 1
fi

# Voice ID — set via env var or replace this default once owner selects a voice
VOICE_ID="${LAIN_VOICE_ID:-}"

if [[ -z "$VOICE_ID" ]]; then
    echo "ERROR: LAIN_VOICE_ID not set. Set it as env var: LAIN_VOICE_ID=<id> bash tts_send.sh" >&2
    echo "Browse voices at https://elevenlabs.io/voice-library and set the voice ID." >&2
    exit 1
fi

# ElevenLabs model — v3 supports expression brackets
MODEL_ID="eleven_v3"

# ── Read text from stdin ──────────────────────────────────────────────────

TEXT=$(cat)

if [[ -z "$TEXT" ]]; then
    echo "ERROR: empty text — pipe content via stdin" >&2
    exit 1
fi

# ── Call ElevenLabs TTS API ───────────────────────────────────────────────

AUDIO_FILE="/tmp/lain_tts_$$.mp3"

HTTP_STATUS=$(curl -s -o "$AUDIO_FILE" -w "%{http_code}" \
    -X POST \
    -H "xi-api-key: $ELEVENLABS_KEY" \
    -H "Content-Type: application/json" \
    "https://api.elevenlabs.io/v1/text-to-speech/${VOICE_ID}" \
    -d "{
        \"text\": $(echo "$TEXT" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'),
        \"model_id\": \"$MODEL_ID\",
        \"voice_settings\": {
            \"stability\": 0.7,
            \"similarity_boost\": 0.8,
            \"style\": 0.3
        }
    }")

if [[ "$HTTP_STATUS" != "200" ]]; then
    echo "ERROR: ElevenLabs API returned status $HTTP_STATUS" >&2
    cat "$AUDIO_FILE" >&2
    rm -f "$AUDIO_FILE"
    exit 1
fi

# ── Send as Telegram voice note ───────────────────────────────────────────

RESPONSE=$(curl -s -X POST \
    "https://api.telegram.org/bot${BOT_TOKEN}/sendVoice" \
    -F "chat_id=${CHAT_ID}" \
    -F "voice=@${AUDIO_FILE};type=audio/mpeg" \
    -F "caption=🎙" \
    )

rm -f "$AUDIO_FILE"

# Check Telegram response
SUCCESS=$(echo "$RESPONSE" | python3 -c "import json,sys; r=json.load(sys.stdin); print('ok' if r.get('ok') else 'fail')" 2>/dev/null)
if [[ "$SUCCESS" == "ok" ]]; then
    MSG_ID=$(echo "$RESPONSE" | python3 -c "import json,sys; r=json.load(sys.stdin); print(r['result']['message_id'])" 2>/dev/null || echo "?")
    echo "tts_send: voice note sent (message_id=$MSG_ID)"
else
    echo "ERROR: Telegram sendVoice failed: $RESPONSE" >&2
    exit 1
fi
