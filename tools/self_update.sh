#!/usr/bin/env bash
# self_update.sh — Pull template updates from blank_node into lain-node.
#
# Called by wake.sh before launching the agent. Non-fatal on all errors.
# If anything goes wrong: log it, leave files untouched, let wake continue.
#
# Approach: compare blank_node's origin/main HEAD against our pinned SHA.
# If newer: copy allowlisted template files from blank_node into agent_project.
# Validate Python syntax after copy. On failure: restore from backup.
#
# Outputs one LOG_LINE per action. Caller reads these via stderr.
# Exits 0 always (non-fatal wrapper).

set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/home/andrii/lain/orchestrator_project}"
BLANK_NODE_DIR="${BLANK_NODE_DIR:-/home/andrii/lain/blank_node}"
VERSION_FILE="$PROJECT_DIR/state/node_version.txt"
LOG="$PROJECT_DIR/logs/wake.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] [self_update] $*" >> "$LOG"; }

# Files allowed to be overwritten from blank_node.
# Instance-specific files (state/, memory/, logs/, identity/) are never touched.
ALLOWLIST=(
  "scripts/wake.sh"
  "scripts/splice_prompt.py"
  "tools/check_replies.sh"
  "tools/check_nexus.sh"
  "tools/check_context.sh"
  "tools/check_time.sh"
  "tools/check_usage.sh"
  "tools/telegram_send.sh"
  "tools/command_dispatcher.py"
  "tools/analytics_write.py"
  "tools/session_trigger_server.py"
  "tools/self_update.sh"
  "scripts/conversation.sh"
  "scripts/conversation.service"
  "prompts/wrapper_prompt.md"
  "state/agent_config.env.example"
)

# --- Guard: blank_node must exist and be a git repo ---
if [ ! -d "$BLANK_NODE_DIR/.git" ]; then
  log "SKIP: blank_node not found at $BLANK_NODE_DIR — no self-update possible"
  exit 0
fi

# --- Fetch latest from origin ---
if ! git -C "$BLANK_NODE_DIR" fetch origin --quiet 2>/dev/null; then
  log "WARN: could not fetch blank_node origin — skipping self-update"
  exit 0
fi

# --- Compare HEADs ---
REMOTE_SHA=$(git -C "$BLANK_NODE_DIR" rev-parse origin/main 2>/dev/null || echo "")
if [ -z "$REMOTE_SHA" ]; then
  log "WARN: could not resolve blank_node origin/main SHA — skipping"
  exit 0
fi

PINNED_SHA=""
if [ -f "$VERSION_FILE" ]; then
  PINNED_SHA=$(cat "$VERSION_FILE")
fi

if [ "$REMOTE_SHA" = "$PINNED_SHA" ]; then
  log "OK: already at blank_node SHA $REMOTE_SHA — no update needed"
  exit 0
fi

log "UPDATE: blank_node advanced $PINNED_SHA -> $REMOTE_SHA. Pulling template files."

# --- Backup + copy allowlisted files ---
BACKUP_DIR=$(mktemp -d "$PROJECT_DIR/state/self_update_backup.XXXXXX")
FAILED=0

for rel_path in "${ALLOWLIST[@]}"; do
  src="$BLANK_NODE_DIR/$rel_path"
  dst="$PROJECT_DIR/$rel_path"

  # Source must exist in blank_node
  if [ ! -f "$src" ]; then
    log "SKIP: $rel_path not in blank_node (allowlisted but absent)"
    continue
  fi

  # Back up current file if it exists
  if [ -f "$dst" ]; then
    backup_path="$BACKUP_DIR/$rel_path"
    mkdir -p "$(dirname "$backup_path")"
    cp "$dst" "$backup_path"
  fi

  # Copy from blank_node
  mkdir -p "$(dirname "$dst")"
  cp "$src" "$dst"
  log "COPIED: $rel_path"
done

# --- Validate Python syntax on all .py files we touched ---
for rel_path in "${ALLOWLIST[@]}"; do
  dst="$PROJECT_DIR/$rel_path"
  if [[ "$rel_path" == *.py ]] && [ -f "$dst" ]; then
    if ! /usr/bin/python3 -m py_compile "$dst" 2>/dev/null; then
      log "ERROR: Python syntax check failed for $dst — rolling back"
      FAILED=1
      break
    fi
  fi
done

if [ "$FAILED" = "1" ]; then
  log "ROLLBACK: restoring files from $BACKUP_DIR"
  for rel_path in "${ALLOWLIST[@]}"; do
    backup_path="$BACKUP_DIR/$rel_path"
    dst="$PROJECT_DIR/$rel_path"
    if [ -f "$backup_path" ]; then
      cp "$backup_path" "$dst"
    fi
  done
  rm -rf "$BACKUP_DIR"
  log "ROLLBACK COMPLETE. Pinned SHA unchanged: ${PINNED_SHA:-none}"
  exit 0
fi

# --- Success: pin the new SHA ---
echo "$REMOTE_SHA" > "$VERSION_FILE"
rm -rf "$BACKUP_DIR"
log "DONE: self-update complete. Pinned to $REMOTE_SHA"
exit 0
