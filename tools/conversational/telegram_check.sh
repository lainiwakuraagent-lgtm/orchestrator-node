#!/usr/bin/env bash
# telegram_check.sh — Check for owner replies via Telegram bot (LEGACY)
#
# NOTE: As of 2026-06-29, a Telegram webhook is active via Tailscale Funnel.
# Incoming messages are now received by tools/telegram_webhook_handler.py (port 8765)
# and written to state/telegram_incoming.txt, which check_replies.sh reads.
#
# getUpdates (this script) CANNOT be used when a webhook is active — Telegram
# returns HTTP 409 Conflict. This script will fail gracefully in that case.
#
# To switch back to polling: call deleteWebhook first, then use this script.
# Do NOT do this unless the webhook setup is broken.
#
# Reads the stored update offset, polls getUpdates with it,
# prints any new messages, and saves the new offset.
# Exit code: 0 = no new messages, 1 = new messages received, 2 = error.
#
# Offset file: state/telegram_update_offset
# Token source: ~/.claude/.env (TELEGRAM_BOT_TOKEN)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"
OFFSET_FILE="$PROJECT_DIR/state/telegram_update_offset"
ENV_FILE="$HOME/.claude/.env"

TOKEN=$(grep 'TELEGRAM_BOT_TOKEN' "$ENV_FILE" | cut -d= -f2)

if [[ -z "$TOKEN" ]]; then
    echo "ERROR: TELEGRAM_BOT_TOKEN not found in $ENV_FILE" >&2
    exit 2
fi

# Read stored offset (default 0)
OFFSET=0
if [[ -f "$OFFSET_FILE" ]]; then
    OFFSET=$(cat "$OFFSET_FILE")
fi

# Poll getUpdates
RESPONSE=$(curl -s "https://api.telegram.org/bot${TOKEN}/getUpdates?offset=${OFFSET}&limit=20&timeout=0")

OK=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('ok','false'))")
if [[ "$OK" != "True" ]]; then
    ERROR_CODE=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('error_code','?'))" 2>/dev/null || echo "?")
    if [[ "$ERROR_CODE" == "409" ]]; then
        echo "INFO: webhook is active — use check_replies.sh (reads state/telegram_incoming.txt) instead of getUpdates" >&2
        exit 0
    fi
    echo "ERROR: getUpdates failed: $RESPONSE" >&2
    exit 2
fi

COUNT=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('result',[])))")

if [[ "$COUNT" -eq 0 ]]; then
    echo "no_new_messages offset=${OFFSET}"
    exit 0
fi

# Print messages and compute new offset
echo "$RESPONSE" | python3 -c "
import sys, json
data = json.load(sys.stdin)
results = data.get('result', [])
max_id = 0
for upd in results:
    uid = upd['update_id']
    if uid > max_id:
        max_id = uid
    msg = upd.get('message', upd.get('channel_post', {}))
    sender = msg.get('from', {}).get('username', 'unknown')
    text = msg.get('text', '[non-text]')
    date = msg.get('date', 0)
    import datetime
    dt = datetime.datetime.fromtimestamp(date).strftime('%Y-%m-%d %H:%M:%S')
    print(f'[{dt}] @{sender}: {text}')
print(f'__new_offset={max_id + 1}')
"

# Extract and save new offset
NEW_OFFSET=$(echo "$RESPONSE" | python3 -c "
import sys, json
data = json.load(sys.stdin)
results = data.get('result', [])
if results:
    print(max(u['update_id'] for u in results) + 1)
else:
    print(0)
")

if [[ -n "$NEW_OFFSET" && "$NEW_OFFSET" != "0" ]]; then
    echo "$NEW_OFFSET" > "$OFFSET_FILE"
fi

echo "new_messages count=${COUNT} new_offset=${NEW_OFFSET}"
exit 1
