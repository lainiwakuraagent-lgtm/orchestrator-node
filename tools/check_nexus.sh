#!/usr/bin/env bash
# check_nexus.sh — Check Nexus channels for new messages since last read.
#
# Reads new messages from subscribed Nexus conversations.
# Tracks per-conversation last-read message ID in state/nexus_last_read.json.
#
# Usage:
#   bash tools/check_nexus.sh
#
# Exit codes:
#   0 — no new messages
#   1 — new messages found (outputs to stdout, appended to conversation log)
#
# Environment:
#   NEXUS_URL (default: http://100.110.36.84:8900)
#   NEXUS_USERNAME (default: orchestrator)
#   NEXUS_PASSWORD (from identity/nexus_seed_passwords.txt if not set)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LAST_READ_FILE="$PROJECT_DIR/state/nexus_last_read.json"
CONV_LOG="$PROJECT_DIR/memory/conversation.md"
NEXUS_URL="${NEXUS_URL:-http://100.110.36.84:8900}"
NEXUS_USERNAME="${NEXUS_USERNAME:-orchestrator}"
TIMESTAMP=$(date '+%Y-%m-%d %H:%M')

# Load password
if [ -z "${NEXUS_PASSWORD:-}" ]; then
    PASS_FILE="$PROJECT_DIR/identity/nexus_seed_passwords.txt"
    if [ -f "$PASS_FILE" ]; then
        NEXUS_PASSWORD=$(grep "^# orchestrator" "$PASS_FILE" | grep -o '[^ ]*$' | head -1)
    fi
fi

if [ -z "${NEXUS_PASSWORD:-}" ]; then
    echo "check_nexus: NEXUS_PASSWORD not configured — skipping"
    exit 0
fi

# Initialize last-read file if missing
if [ ! -f "$LAST_READ_FILE" ]; then
    echo "{}" > "$LAST_READ_FILE"
fi

# Get JWT token
TOKEN=$(curl -s --max-time 5 -X POST "$NEXUS_URL/auth/token" \
    -H "Content-Type: application/json" \
    -d "{\"username\":\"$NEXUS_USERNAME\",\"password\":\"$NEXUS_PASSWORD\"}" \
    | /usr/bin/python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('access_token',''))" 2>/dev/null || echo "")

if [ -z "$TOKEN" ]; then
    echo "check_nexus: could not authenticate — is Nexus running? Skipping."
    exit 0
fi

# Check for new messages using Python (handles JSON state cleanly)
/usr/bin/python3 - "$TOKEN" "$NEXUS_URL" "$LAST_READ_FILE" "$CONV_LOG" "$TIMESTAMP" << 'PYEOF'
import json, sys, urllib.request, urllib.error, os, datetime

token, nexus_url, last_read_file, conv_log, timestamp = sys.argv[1:]

headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

def api_get(path):
    req = urllib.request.Request(f"{nexus_url}{path}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())
    except Exception:
        return []

# Get own agent ID to filter self-messages
me = api_get("/auth/me")
my_id = me.get("id", "") if isinstance(me, dict) else ""

# Build agent ID → username lookup cache
_agent_cache = {}
def username_for(agent_id):
    if not agent_id:
        return "?"
    if agent_id == my_id:
        return "orchestrator"
    if agent_id not in _agent_cache:
        a = api_get(f"/agents/{agent_id}")
        _agent_cache[agent_id] = a.get("username", agent_id[:8]) if isinstance(a, dict) else agent_id[:8]
    return _agent_cache[agent_id]

# Load last-read state
with open(last_read_file) as f:
    last_read = json.load(f)

# Load conversation state
project_dir = os.path.dirname(os.path.dirname(last_read_file))
conv_state_file = os.path.join(project_dir, "state", "nexus_conversation_state.json")
AGENT_PEERS = {"asuka", "echidna", "lain"}
if os.path.exists(conv_state_file):
    with open(conv_state_file) as f:
        conv_state = json.load(f)
else:
    conv_state = {"updated_at": "", "active_threads": {}}
conv_state_changed = False

# Get all conversations I'm in
conversations = api_get("/conversations/")
new_messages_found = False

new_last_read = dict(last_read)

for conv in conversations:
    conv_id = conv["id"]
    conv_name = conv.get("name") or conv.get("type", "unknown")

    # Get recent messages
    params = "?limit=20"
    messages = api_get(f"/conversations/{conv_id}/messages{params}")

    if not messages:
        continue

    # Filter to messages newer than last read
    last_id = last_read.get(conv_id, "")
    new_msgs = []

    # NOTE: API returns messages newest-first (messages[0] = most recent).
    if last_id:
        # Take everything BEFORE last_id in the list — those are newer messages.
        found = False
        for msg in messages:
            if msg["id"] == last_id:
                found = True
                break
            new_msgs.append(msg)
        if not found:
            # last_id not in this page — all messages on this page are newer.
            new_msgs = messages[:]
    else:
        # First run — take all messages.
        new_msgs = messages[:]

    # Filter out own messages and routine telemetry (relationship state updates, etc.)
    new_msgs = [m for m in new_msgs if m.get("sender_id") != my_id
                and not m.get("content", "").startswith("RELATIONSHIP_STATE")]

    if new_msgs:
        new_messages_found = True
        print(f"=== NEW NEXUS MESSAGES in #{conv_name}: {len(new_msgs)} ===")
        for msg in new_msgs:
            sender = username_for(msg.get("sender_id", ""))
            content = msg.get("content", "")
            print(f"  [{msg.get('created_at','?')[:16]}] @{sender}: {content}")
            # Append to conversation log
            with open(conv_log, "a") as f:
                f.write(f"\n[{timestamp}] {sender.upper()} via Nexus ({conv_name}) | {content}\n")
            # Update conversation state for agent-peer messages
            if sender in AGENT_PEERS:
                thread = conv_state["active_threads"].setdefault(sender, {
                    "channel": conv_name, "last_sent_id": None,
                    "last_received_id": None, "waiting_for_reply": False,
                    "thread_topic": None, "last_activity": None, "last_5_messages": []
                })
                thread["last_received_id"] = msg["id"]
                thread["last_activity"] = msg.get("created_at", "")
                thread["channel"] = conv_name
                thread["last_5_messages"].append({
                    "from": sender, "text": content[:200],
                    "time": msg.get("created_at", "")
                })
                thread["last_5_messages"] = thread["last_5_messages"][-5:]
                conv_state_changed = True
        print("=" * 40)

    # Update last-read to the newest message (messages[0] since newest-first order).
    if messages:
        new_last_read[conv_id] = messages[0]["id"]

# Save updated last-read state
with open(last_read_file, "w") as f:
    json.dump(new_last_read, f, indent=2)

# Save conversation state if updated
if conv_state_changed:
    conv_state["updated_at"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(conv_state_file, "w") as f:
        json.dump(conv_state, f, indent=2)

if not new_messages_found:
    print("Nexus: no new messages")
    sys.exit(0)
else:
    sys.exit(1)
PYEOF
