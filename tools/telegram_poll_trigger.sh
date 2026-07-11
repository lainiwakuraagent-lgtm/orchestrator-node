#!/usr/bin/env bash
# telegram_poll_trigger.sh — Poll Telegram for new messages, trigger session if found.
#
# This is the orchestrator's primary wake mechanism.
# Run by a systemd timer every 5 minutes.
#
# Flow:
#   1. Call Telegram getUpdates with stored offset
#   2. If new messages found: write to state/telegram_incoming.txt, trigger wake.sh
#   3. If no messages: exit quietly (no session started)
#
# Offset file: state/telegram_update_offset
# Token source: identity/agent.env

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
AGENT_ENV="$PROJECT_DIR/identity/agent.env"
STATE_DIR="$PROJECT_DIR/state"
LOG_DIR="$PROJECT_DIR/logs"
OFFSET_FILE="$STATE_DIR/telegram_update_offset"
INCOMING_FILE="$STATE_DIR/telegram_incoming.txt"
LOCK_FILE="$STATE_DIR/session.lock"

mkdir -p "$STATE_DIR" "$LOG_DIR"

timestamp() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(timestamp)] telegram_poll: $*" >> "$LOG_DIR/wake.log"; }

if [[ ! -f "$AGENT_ENV" ]]; then
    log "ERROR: $AGENT_ENV not found — cannot poll"
    exit 1
fi

TOKEN=$(grep 'TELEGRAM_BOT_TOKEN' "$AGENT_ENV" | cut -d= -f2)
CHAT_ID=$(grep 'TELEGRAM_CHAT_ID' "$AGENT_ENV" | cut -d= -f2)

OFFSET=0
if [[ -f "$OFFSET_FILE" ]]; then
    OFFSET=$(cat "$OFFSET_FILE")
fi

# Fetch updates from Telegram
RESPONSE=$(curl -s "https://api.telegram.org/bot${TOKEN}/getUpdates?offset=${OFFSET}&timeout=0&limit=10")
OK=$(echo "$RESPONSE" | /usr/bin/python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('ok',False))" 2>/dev/null || echo "False")

if [[ "$OK" != "True" ]]; then
    log "ERROR: getUpdates failed — $RESPONSE"
    exit 1
fi

# Extract messages from allowed chat_id only
NEW_MESSAGES=$(/usr/bin/python3 - "$RESPONSE" "$CHAT_ID" "$OFFSET_FILE" << 'PYEOF'
import sys, json

response_str = sys.argv[1]
allowed_chat = int(sys.argv[2])
offset_file = sys.argv[3]

data = json.loads(response_str)
results = data.get('result', [])

messages = []
max_update_id = None

for update in results:
    uid = update['update_id']
    if max_update_id is None or uid > max_update_id:
        max_update_id = uid

    msg = update.get('message') or update.get('edited_message')
    if not msg:
        continue
    chat_id = msg.get('chat', {}).get('id')
    if chat_id != allowed_chat:
        continue
    from_user = msg.get('from', {}).get('username', 'unknown')
    text = msg.get('text', '').strip()
    if not text:
        continue
    ts = msg.get('date', 0)
    from datetime import datetime, timezone
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')
    messages.append(f"{dt} | @{from_user} | chat={chat_id} | {text}")

if max_update_id is not None:
    with open(offset_file, 'w') as f:
        f.write(str(max_update_id + 1))

if messages:
    print('\n'.join(messages))
PYEOF
)

if [[ -z "$NEW_MESSAGES" ]]; then
    log "No new messages. No session triggered."
    exit 0
fi

# New messages found — write to incoming file
log "New messages found. Writing to $INCOMING_FILE."
echo "$NEW_MESSAGES" > "$INCOMING_FILE"

# Check if a session is already running
if [[ -f "$LOCK_FILE" ]]; then
    locked_pid=$(cat "$LOCK_FILE" 2>/dev/null || echo "")
    if [[ -n "$locked_pid" ]] && kill -0 "$locked_pid" 2>/dev/null; then
        log "Session already running (PID $locked_pid) — messages queued in $INCOMING_FILE."
        exit 0
    fi
fi

# Trigger session
log "Triggering wake.sh (Telegram-driven)."
TRIGGER_MODE=manual PROJECT_DIR="$PROJECT_DIR" \
    bash "$PROJECT_DIR/scripts/wake.sh" \
    "$PROJECT_DIR/prompts/goal.txt" \
    "$PROJECT_DIR/prompts/persona.txt" \
    >> "$LOG_DIR/session.out" 2>> "$LOG_DIR/session.err" &

log "Session launched (PID $!)."
