#!/usr/bin/env bash
# telegram_send.sh — Send a message to the owner via Telegram bot
#
# Usage: telegram_send.sh "message text"
#   or:  telegram_send.sh  (reads message from stdin)
#
# Prints the message_id of the sent message on success.
# Token source: ~/.claude/.env (TELEGRAM_BOT_TOKEN)
# Chat ID source: ~/.claude/.env (TELEGRAM_ALLOWED_USERS)

set -euo pipefail

# Allow CURL_CMD override for testing (e.g. CURL_CMD=/path/to/stub)
CURL="${CURL_CMD:-curl}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"
AGENT_ENV="$PROJECT_DIR/identity/agent.env"
FALLBACK_ENV="$HOME/.claude/.env"

# Token resolution (same priority as telegram_watcher.py):
#   1. TELEGRAM_BOT_TOKEN_FILE env var → read from file
#   2. TELEGRAM_BOT_TOKEN env var (direct)
#   3. identity/agent.env → TELEGRAM_BOT_TOKEN
#   4. ~/.claude/.env → TELEGRAM_BOT_TOKEN
TOKEN=""
if [[ -n "${TELEGRAM_BOT_TOKEN_FILE:-}" && -f "${TELEGRAM_BOT_TOKEN_FILE}" ]]; then
    TOKEN=$(cat "$TELEGRAM_BOT_TOKEN_FILE" | tr -d '[:space:]')
fi
if [[ -z "$TOKEN" && -n "${TELEGRAM_BOT_TOKEN:-}" ]]; then
    TOKEN="$TELEGRAM_BOT_TOKEN"
fi
if [[ -z "$TOKEN" && -f "$AGENT_ENV" ]]; then
    TOKEN=$(grep '^TELEGRAM_BOT_TOKEN=' "$AGENT_ENV" 2>/dev/null | cut -d= -f2)
fi
if [[ -z "$TOKEN" ]]; then
    TOKEN=$(grep 'TELEGRAM_BOT_TOKEN' "$FALLBACK_ENV" 2>/dev/null | cut -d= -f2)
fi

# Chat ID resolution:
#   1. TELEGRAM_CHAT_ID env var (direct)
#   2. identity/agent.env → TELEGRAM_CHAT_ID or TELEGRAM_ALLOWED_USERS
#   3. ~/.claude/.env → TELEGRAM_ALLOWED_USERS
CHAT_ID="${TELEGRAM_CHAT_ID:-}"
if [[ -z "$CHAT_ID" && -f "$AGENT_ENV" ]]; then
    CHAT_ID=$(grep '^TELEGRAM_CHAT_ID=' "$AGENT_ENV" 2>/dev/null | cut -d= -f2)
    if [[ -z "$CHAT_ID" ]]; then
        CHAT_ID=$(grep '^TELEGRAM_ALLOWED_USERS=' "$AGENT_ENV" 2>/dev/null | cut -d= -f2)
    fi
fi
if [[ -z "$CHAT_ID" ]]; then
    CHAT_ID=$(grep 'TELEGRAM_ALLOWED_USERS' "$FALLBACK_ENV" 2>/dev/null | cut -d= -f2)
fi

if [[ -z "$TOKEN" ]]; then
    echo "ERROR: TELEGRAM_BOT_TOKEN not found (checked env vars, $AGENT_ENV, $FALLBACK_ENV)" >&2
    exit 2
fi

if [[ -z "$CHAT_ID" ]]; then
    echo "ERROR: TELEGRAM_CHAT_ID not found (checked env vars, $AGENT_ENV, $FALLBACK_ENV)" >&2
    exit 2
fi

# Get message from arg or stdin
if [[ $# -ge 1 ]]; then
    MESSAGE="$*"
else
    MESSAGE=$(cat)
fi

if [[ -z "$MESSAGE" ]]; then
    echo "ERROR: No message provided" >&2
    exit 2
fi

RESPONSE=$($CURL -s -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${CHAT_ID}" \
    --data-urlencode "text=${MESSAGE}")

OK=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('ok','false'))")
if [[ "$OK" != "True" ]]; then
    echo "ERROR: sendMessage failed: $RESPONSE" >&2
    exit 2
fi

MSG_ID=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['result']['message_id'])")
echo "sent message_id=${MSG_ID}"

# ── TTS (optional) ─────────────────────────────────────────────────────────────
VOICE_MODE_FILE="$(dirname "$(dirname "${BASH_SOURCE[0]}")")/state/voice_mode.txt"
FISH_TTS="$(dirname "${BASH_SOURCE[0]}")/fish_tts_send.sh"
if [[ "${SKIP_TTS:-0}" != "1" && -f "$VOICE_MODE_FILE" && "$(cat "$VOICE_MODE_FILE" 2>/dev/null | tr -d '[:space:]')" == "on" && -f "$FISH_TTS" ]]; then
    printf '%s' "$MESSAGE" | bash "$FISH_TTS" 2>/dev/null || true
fi
