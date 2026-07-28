#!/usr/bin/env bash
# home_tts_play.sh — Convert text to speech and play locally on the home agent.
#
# Mode A (ElevenLabs): calls ElevenLabs API → downloads mp3 → plays via mpg123.
# Mode B (espeak-ng):  local fallback when ELEVENLABS_API_KEY is unset.
#
# Usage: echo "text to speak" | bash scripts/home_tts_play.sh
#    OR: printf '%s' "text" | bash scripts/home_tts_play.sh
#
# Config (identity/agent.env):
#   ELEVENLABS_API_KEY  — API key. If unset, falls back to espeak-ng.
#   HOME_AGENT_VOICE_ID — ElevenLabs voice ID. Set after T264 persona decision.
#                         Falls back to LAIN_VOICE_ID env var.
#
# Exit codes:
#   0 — speech played successfully
#   1 — error

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"
AGENT_ENV="$PROJECT_DIR/identity/agent.env"

# ── Load config ───────────────────────────────────────────────────────────────

load_agent_env_var() {
    local key="$1"
    grep "^${key}=" "$AGENT_ENV" 2>/dev/null | cut -d= -f2- | head -1 | tr -d '[:space:]' || true
}

ELEVENLABS_KEY="${ELEVENLABS_API_KEY:-$(load_agent_env_var ELEVENLABS_API_KEY)}"
VOICE_ID="${HOME_AGENT_VOICE_ID:-$(load_agent_env_var HOME_AGENT_VOICE_ID)}"
VOICE_ID="${VOICE_ID:-${LAIN_VOICE_ID:-}}"

# ElevenLabs model with expression support
MODEL_ID="eleven_v3"

# ── Read text from stdin ──────────────────────────────────────────────────────

TEXT=$(cat)

if [ -z "$TEXT" ]; then
    echo "ERROR: empty text — pipe content via stdin" >&2
    exit 1
fi

# ── Choose mode ───────────────────────────────────────────────────────────────

if [ -z "$ELEVENLABS_KEY" ]; then
    # Mode B: espeak-ng fallback (no API key set)
    if ! command -v espeak-ng &>/dev/null; then
        echo "ERROR: espeak-ng not installed and ELEVENLABS_API_KEY not set." >&2
        echo "Install: sudo apt install espeak-ng" >&2
        exit 1
    fi
    echo "$TEXT" | espeak-ng --stdin 2>/dev/null
    echo "[home_tts] played via espeak-ng (dev mode — set ELEVENLABS_API_KEY for ElevenLabs)"
    exit 0
fi

# Mode A: ElevenLabs TTS → mpg123 playback
if [ -z "$VOICE_ID" ]; then
    echo "ERROR: HOME_AGENT_VOICE_ID not set in $AGENT_ENV (needed for ElevenLabs mode)" >&2
    echo "Set: HOME_AGENT_VOICE_ID=<voice_id>  # from T264 persona decision" >&2
    exit 1
fi

if ! command -v mpg123 &>/dev/null; then
    echo "ERROR: mpg123 not installed. Run: sudo apt install mpg123" >&2
    exit 1
fi

# ── Call ElevenLabs TTS API ───────────────────────────────────────────────────

AUDIO_FILE="/tmp/home_tts_$$.mp3"

# JSON-encode text safely (avoids injection via stdin pipe to python3)
JSON_BODY=$(/usr/bin/python3 -c "
import json, sys
text = sys.stdin.read()
print(json.dumps({
    'text': text,
    'model_id': '$MODEL_ID',
    'voice_settings': {
        'stability': 0.7,
        'similarity_boost': 0.8,
        'style': 0.3,
    },
}))
" <<< "$TEXT")

HTTP_STATUS=$(curl -s -o "$AUDIO_FILE" -w "%{http_code}" \
    --max-time 30 \
    -X POST \
    -H "xi-api-key: $ELEVENLABS_KEY" \
    -H "Content-Type: application/json" \
    "https://api.elevenlabs.io/v1/text-to-speech/${VOICE_ID}" \
    -d "$JSON_BODY")

if [ "$HTTP_STATUS" != "200" ]; then
    echo "ERROR: ElevenLabs API returned status $HTTP_STATUS" >&2
    cat "$AUDIO_FILE" >&2 2>/dev/null || true
    rm -f "$AUDIO_FILE"
    exit 1
fi

# ── Play locally ──────────────────────────────────────────────────────────────

mpg123 -q "$AUDIO_FILE" 2>/dev/null
rm -f "$AUDIO_FILE"
echo "[home_tts] played via ElevenLabs (voice=$VOICE_ID)"
