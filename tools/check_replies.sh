#!/usr/bin/env bash
# check_replies.sh — Check all owner reply channels and log new messages
#
# Checks:
#   1. /home/andrii/reply.txt (file-based reply channel)
#   2. Telegram webhook incoming (state/telegram_incoming.txt)
#      [webhook handler: tools/telegram_webhook_handler.py via systemd]
#   3. Nexus agent messenger (tools/check_nexus.sh)
#
# On finding new messages:
#   - Appends to memory/conversation.md
#   - Archives reply.txt to memory/work/replies/ with timestamp
#   - Clears telegram_incoming.txt after reading
#   - Exits 0 if no new messages, 1 if new messages found
#
# Run this at the START of every session before doing other work.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
CONV_LOG="$PROJECT_DIR/memory/conversation.md"
REPLY_FILE="$HOME/reply.txt"
REPLY_ARCHIVE_DIR="$PROJECT_DIR/memory/work/replies"
TELEGRAM_INCOMING="$PROJECT_DIR/state/telegram_incoming.txt"

mkdir -p "$REPLY_ARCHIVE_DIR"

NEW_MESSAGES=0
TIMESTAMP=$(date '+%Y-%m-%d %H:%M')

# ── Channel 1: reply.txt ────────────────────────────────────────────────────

if [[ -f "$REPLY_FILE" && -s "$REPLY_FILE" ]]; then
    REPLY_CONTENT=$(cat "$REPLY_FILE")
    echo "=== NEW MESSAGE via reply.txt ==="
    echo "$REPLY_CONTENT"
    echo "================================="

    # Append to conversation log
    printf '\n[%s] ANDRII via reply.txt | %s\n' "$TIMESTAMP" "$REPLY_CONTENT" >> "$CONV_LOG"

    # Archive reply.txt with timestamp
    ARCHIVE_NAME="reply_$(date '+%Y%m%d_%H%M%S').txt"
    cp "$REPLY_FILE" "$REPLY_ARCHIVE_DIR/$ARCHIVE_NAME"

    # Clear reply.txt so next session doesn't re-read it
    > "$REPLY_FILE"

    NEW_MESSAGES=1
fi

# ── Channel 2: Telegram webhook incoming ────────────────────────────────────
# Messages delivered by Telegram to our webhook handler (tools/telegram_webhook_handler.py)
# are written to state/telegram_incoming.txt. Read and clear the file here.

if [[ -f "$TELEGRAM_INCOMING" && -s "$TELEGRAM_INCOMING" ]]; then
    TG_CONTENT=$(cat "$TELEGRAM_INCOMING")
    LINE_COUNT=$(wc -l < "$TELEGRAM_INCOMING")
    echo "=== NEW TELEGRAM MESSAGES via webhook: $LINE_COUNT ==="
    echo "$TG_CONTENT"
    echo "================================="

    # Append to conversation log
    while IFS= read -r line; do
        printf '\n[%s] ANDRII via Telegram (webhook) | %s\n' "$TIMESTAMP" "$line" >> "$CONV_LOG"
    done < "$TELEGRAM_INCOMING"

    # Clear the file so next session doesn't re-read
    > "$TELEGRAM_INCOMING"

    NEW_MESSAGES=1
else
    echo "Telegram webhook: no new messages"
fi

# ── Channel 3: Nexus ─────────────────────────────────────────────────────────

if bash "$SCRIPT_DIR/check_nexus.sh"; then
    : # no new messages
else
    NEXUS_EXIT=$?
    if [[ "$NEXUS_EXIT" -eq 1 ]]; then
        NEW_MESSAGES=1
    fi
fi

# ── Summary ──────────────────────────────────────────────────────────────────

if [[ "$NEW_MESSAGES" -eq 0 ]]; then
    echo "check_replies: no new messages on any channel"
    exit 0
else
    echo "check_replies: NEW MESSAGES FOUND — read above, conversation log updated"
    exit 1
fi
