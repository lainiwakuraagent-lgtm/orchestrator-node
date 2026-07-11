#!/usr/bin/env bash
# telegram_send.sh — Send a message to Andrii via orchestrator Telegram bot
#
# Usage: telegram_send.sh "message text"
#   or:  telegram_send.sh  (reads message from stdin)
#
# Token source: PROJECT_DIR/identity/agent.env (TELEGRAM_BOT_TOKEN)
# Chat ID source: PROJECT_DIR/identity/agent.env (TELEGRAM_CHAT_ID)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
AGENT_ENV="$PROJECT_DIR/identity/agent.env"

if [[ ! -f "$AGENT_ENV" ]]; then
    echo "ERROR: $AGENT_ENV not found" >&2
    exit 2
fi

TOKEN=$(grep 'TELEGRAM_BOT_TOKEN' "$AGENT_ENV" | cut -d= -f2)
CHAT_ID=$(grep 'TELEGRAM_CHAT_ID' "$AGENT_ENV" | cut -d= -f2)

if [[ -z "$TOKEN" ]]; then
    echo "ERROR: TELEGRAM_BOT_TOKEN not found in $AGENT_ENV" >&2
    exit 2
fi
if [[ -z "$CHAT_ID" ]]; then
    echo "ERROR: TELEGRAM_CHAT_ID not found in $AGENT_ENV" >&2
    exit 2
fi

if [[ $# -ge 1 ]]; then
    MESSAGE="$*"
else
    MESSAGE=$(cat)
fi

if [[ -z "$MESSAGE" ]]; then
    echo "ERROR: No message provided" >&2
    exit 2
fi

CURL="${CURL_CMD:-curl}"
RESPONSE=$($CURL -s -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${CHAT_ID}" \
    --data-urlencode "text=${MESSAGE}" \
    --data-urlencode "parse_mode=Markdown")

OK=$(echo "$RESPONSE" | /usr/bin/python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('ok','false'))")
if [[ "$OK" != "True" ]]; then
    echo "ERROR: sendMessage failed: $RESPONSE" >&2
    exit 2
fi

MSG_ID=$(echo "$RESPONSE" | /usr/bin/python3 -c "import sys,json; d=json.load(sys.stdin); print(d['result']['message_id'])")
echo "sent message_id=${MSG_ID}"
