#!/usr/bin/env bash
# wake.sh
# Invoked by systemd timers or the manual trigger server.
# Supports three launch modes via TRIGGER_MODE env var:
#   nightly   (default) — scheduled night sessions, window + trigger gate enforcement
#   emergency           — emergency/daytime mode, window gates bypassed
#   manual              — owner-initiated trigger, window gates bypassed
#
# Usage: wake.sh [persona_file]
#
# The goal is no longer a static file — Loom is the sole goal/project source.
# The active goal/project (see loom's GoalService.resolve_active()) is
# resolved from the Loom DB early in this script and used to build the
# session's base goal text dynamically. This replaces the old
# goal.txt / emergency_goal.txt / default_goal.txt files entirely.

set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
cd "$PROJECT_DIR"
STATE_DIR="$PROJECT_DIR/state"
LOG_DIR="$PROJECT_DIR/logs"
WRAPPER_TEMPLATE="$PROJECT_DIR/prompts/wrapper_prompt.md"
EMERGENCY_FLAG="$STATE_DIR/emergency_mode.active"

# --- Load agent config (parameterize for new node instances) ---
AGENT_CONFIG="$STATE_DIR/agent_config.env"
if [ -f "$AGENT_CONFIG" ]; then
  # shellcheck disable=SC1090
  source "$AGENT_CONFIG"
fi
AGENT_NAME="${AGENT_NAME:-UNCONFIGURED_AGENT}"
OWNER_NAME="${OWNER_NAME:-UNCONFIGURED_OWNER}"
if [ "$AGENT_NAME" = "UNCONFIGURED_AGENT" ] || [ "$OWNER_NAME" = "UNCONFIGURED_OWNER" ]; then
  echo "WARNING: AGENT_NAME or OWNER_NAME not set in $AGENT_CONFIG. Copy state/agent_config.env.example and configure it." >&2
fi
AGENT_REPO="${AGENT_REPO:-lainiwakuraagent-lgtm/node}"
NODE_VERSION="${NODE_VERSION:-claude-sonnet-4-6}"

PERSONA_FILE="${1:-}"

mkdir -p "$STATE_DIR" "$LOG_DIR"

timestamp() { date '+%Y-%m-%d %H:%M:%S %Z'; }
log_line() { echo "[$(timestamp)] $*" >> "$LOG_DIR/wake.log"; }

# --- Trigger result reporting (no-op unless TRIGGER_REQUEST_ID is set) ---
# The manual-trigger HTTP endpoints (session_trigger_server.py, web_server.py)
# launch this script fire-and-forget and have no other way to learn whether a
# session actually started -- they'd otherwise report "triggered" the instant
# the subprocess spawns, even if a gate below silently skips or aborts it.
# When set, TRIGGER_REQUEST_ID is echoed back in this file so the caller can
# match its own request to the right result (atomic write via tmp+replace,
# same pattern as check_window.py's mark-fired).
write_trigger_result() {
  [ -z "${TRIGGER_REQUEST_ID:-}" ] && return 0
  local decision="$1" reason="$2" stype="${3:-}" rpid="${4:-}"
  python3 -c '
import json, os, sys
request_id, decision, reason, session_type, pid, out_path = sys.argv[1:7]
data = {
    "request_id": request_id,
    "decision": decision,
    "reason": reason,
    "session_type": session_type or None,
    "pid": int(pid) if pid else None,
}
tmp = out_path + ".tmp"
with open(tmp, "w") as f:
    json.dump(data, f)
os.replace(tmp, out_path)
' "$TRIGGER_REQUEST_ID" "$decision" "$reason" "$stype" "$rpid" \
    "$STATE_DIR/manual_trigger_result.json" 2>/dev/null || true
}

# --- Log rotation (non-fatal) ---
# If wake.log exceeds 1MB, rotate it before writing this session's entries.
# Keeps the last 3 rotated files; older ones are removed automatically.
{
  _wake_log="$LOG_DIR/wake.log"
  _wake_log_size=$(stat -c%s "$_wake_log" 2>/dev/null || echo 0)
  if [ "$_wake_log_size" -gt 1048576 ]; then
    _rotated="$LOG_DIR/wake.log.$(date +%Y%m%d_%H%M%S).gz"
    if gzip -c "$_wake_log" > "$_rotated"; then
      : > "$_wake_log"
      # Remove all but the 3 most recent rotated files
      # shellcheck disable=SC2012
      ls -t "$LOG_DIR"/wake.log.*.gz 2>/dev/null | tail -n +4 | xargs -r rm -f
      log_line "LOG ROTATED: was $_wake_log_size bytes → $_rotated (keeping last 3)."
    fi
  fi
  unset _wake_log _wake_log_size _rotated
} || true

# --- Determine trigger mode ---
TRIGGER_MODE="${TRIGGER_MODE:-nightly}"
case "$TRIGGER_MODE" in
  nightly|emergency|manual) ;;
  *)
    log_line "ERROR: unknown TRIGGER_MODE '$TRIGGER_MODE'. Defaulting to nightly."
    TRIGGER_MODE="nightly"
    ;;
esac

log_line "Wake called. TRIGGER_MODE=$TRIGGER_MODE"

# --- Night ID (used for log file naming and Loom session recording) ---
hour=$(date +%H); hour=$((10#$hour))
if [ "$hour" -lt 6 ]; then
  night_id=$(date -d "yesterday" +%Y-%m-%d)
else
  night_id=$(date +%Y-%m-%d)
fi

# Session counter — purely informational, no cap enforced.
COUNT_FILE="$STATE_DIR/sessions_tonight.count"
DATE_FILE="$STATE_DIR/sessions_tonight.date"
last_recorded_night=""
if [ -f "$DATE_FILE" ]; then
  last_recorded_night=$(cat "$DATE_FILE")
fi
if [ "$last_recorded_night" != "$night_id" ]; then
  echo "0" > "$COUNT_FILE"
  echo "$night_id" > "$DATE_FILE"
  echo "0" > "$STATE_DIR/consecutive_philosophy.count"
  log_line "New night detected ($night_id). Session counter reset to 0."
fi
current_count=$(cat "$COUNT_FILE" 2>/dev/null || echo "0")

# --- Gate 0: subscription usage limits (nightly + emergency) ---
# manual is a deliberate break-glass override: it exists for exactly the case
# where a session is needed despite hitting a usage limit, so it skips this
# gate entirely rather than being fail-open on error like the other modes.
# This is what distinguishes it from emergency mode, which bypasses the time
# window but still respects usage limits.
if [ "$TRIGGER_MODE" = "manual" ]; then
  log_line "Gate 0 (usage check) bypassed -- TRIGGER_MODE=manual (break-glass override)."
else
  # Fail-open on errors so network/auth issues never silently kill the launch.
  usage_check_output=$(bash "$PROJECT_DIR/tools/executional/check_session.sh" --usage 2>&1) \
    || usage_check_output="ACTION: cannot check usage -- treat as unknown, proceed with caution."
  usage_action=$(echo "$usage_check_output" | grep '^ACTION:' | head -n1)

  if echo "$usage_action" | grep -q 'usage limit exceeded'; then
    log_line "ABORT: subscription usage too high. check_session.sh --usage output: $usage_check_output"
    exit 0
  fi
  if echo "$usage_action" | grep -q 'cannot check usage'; then
    log_line "WARNING: could not check usage limits (proceeding). check_session.sh --usage output: $usage_check_output"
  fi
fi

# --- Gate 1 (nightly only): block if emergency mode is active ---
# Emergency mode owns the schedule when active; nightly sessions step aside.
if [ "$TRIGGER_MODE" = "nightly" ]; then
  if [ -f "$EMERGENCY_FLAG" ]; then
    reason=$(head -1 "$EMERGENCY_FLAG")
    log_line "ABORT: emergency mode is active ($reason) — nightly session skipped. Disable emergency mode to resume nightly schedule."
    exit 0
  fi
fi

# --- Gate 2 (nightly only): window + trigger check from session_schedule.json ---
# Emergency and manual modes bypass window checks entirely.
# Logic lives in scripts/check_window.py. `check` is read-only — a matching
# one_off entry is detected but NOT marked fired here. It's only marked fired
# after Gate 4 (lock check) passes, below, so a one_off session can never be
# silently consumed without actually launching.
ONE_OFF_INDEX=""
if [ "$TRIGGER_MODE" = "nightly" ]; then
  SCHEDULE_FILE="$PROJECT_DIR/config/session_schedule.json"
  LOCK_FILE_PRE="$STATE_DIR/session.lock"

  window_result=$(python3 "$PROJECT_DIR/scripts/executional/check_window.py" check \
    --schedule-file "$SCHEDULE_FILE" --lock-file "$LOCK_FILE_PRE" 2>&1)

  log_line "Window check result: $window_result"

  window_launch=$(echo "$window_result" | grep '^LAUNCH:' | head -1 | awk '{print $2}')
  WINDOW_TYPE=$(echo "$window_result" | grep '^WINDOW_TYPE:' | head -1 | awk '{print $2}')
  ONE_OFF_INDEX=$(echo "$window_result" | grep '^one_off_index:' | head -1 | awk '{print $2}' || true)
  export WINDOW_TYPE
  log_line "Window type: $WINDOW_TYPE"

  if [ "$window_launch" != "yes" ]; then
    log_line "ABORT: window gate denied launch. $window_result"
    exit 0
  fi
else
  log_line "Gate 2 (window check) skipped — TRIGGER_MODE=$TRIGGER_MODE."
fi

# --- Gate 3 retired: session-count hard cap was downgraded to informational-only
# (see `current_count` above — tracked but no longer enforced). Numbering below
# kept as "Gate 4" for continuity with existing logs/diagrams rather than
# renumbering everything for a cosmetic gap. ---

# --- Gate 4: no session already running (all modes) ---
LOCK_FILE="$STATE_DIR/session.lock"
if [ -f "$LOCK_FILE" ]; then
  locked_pid=$(cat "$LOCK_FILE" 2>/dev/null || echo "")
  if [ -n "$locked_pid" ] && kill -0 "$locked_pid" 2>/dev/null; then
    log_line "SKIP: session already running (PID $locked_pid). Skipping this wake."
    write_trigger_result "skipped" "session already running (PID $locked_pid)"
    exit 0
  else
    log_line "WARNING: stale lock found (PID ${locked_pid:-unknown} is dead). Removing and proceeding."
    rm -f "$LOCK_FILE"
  fi
fi

# Gate 4 has passed — safe now to consume the one_off entry detected in Gate 2.
# For nightly mode, ONE_OFF_INDEX was already set by the Gate 2 check above.
# For manual/emergency mode, Gate 2 is skipped so we do a one-off-only check here.
if [ "$TRIGGER_MODE" != "nightly" ] && [ -z "$ONE_OFF_INDEX" ]; then
  _schedule_file="$PROJECT_DIR/config/session_schedule.json"
  if [ -f "$_schedule_file" ]; then
    _check_out=$(python3 "$PROJECT_DIR/scripts/executional/check_window.py" check \
      --schedule-file "$_schedule_file" --lock-file /dev/null 2>/dev/null || true)
    ONE_OFF_INDEX=$(echo "$_check_out" | grep '^one_off_index:' | head -1 | awk '{print $2}' || true)
    SCHEDULE_FILE="$_schedule_file"
    if [ -n "$ONE_OFF_INDEX" ]; then
      log_line "One-off entry detected in $TRIGGER_MODE mode (index $ONE_OFF_INDEX)."
    fi
    unset _check_out _schedule_file
  fi
fi
if [ -n "$ONE_OFF_INDEX" ]; then
  python3 "$PROJECT_DIR/scripts/executional/check_window.py" mark-fired \
    --schedule-file "$SCHEDULE_FILE" --one-off-index "$ONE_OFF_INDEX" 2>&1 \
    | while IFS= read -r _line; do log_line "$_line"; done
fi

# --- Pre-launch: clean up any stale temp files from crashed previous sessions ---
# The lock gate above confirms no session is running, so leftover temps are safe to remove.
for _stale_pattern in \
    "$STATE_DIR/session_prompt.*.md" \
    "$STATE_DIR/augmented_goal.*.md" \
    "$STATE_DIR/conv_prompt.*.md" \
    "$STATE_DIR/session_type_result.*.json"; do
  # shellcheck disable=SC2086
  _stale_count=$(ls $_stale_pattern 2>/dev/null | wc -l; exit 0)
  if [ "$_stale_count" -gt 0 ]; then
    # shellcheck disable=SC2086
    rm -f $_stale_pattern
    log_line "Cleaned up $_stale_count stale temp file(s) matching $(basename "$_stale_pattern")."
  fi
done
unset _stale_pattern _stale_count

# --- All gates passed: launch the agent ---
new_count=$((current_count + 1))
log_line "LAUNCHING session #$new_count (mode=$TRIGGER_MODE, night=$night_id)."

# Build prompt by splicing goal (and optional persona) into the wrapper template.
session_prompt=$(mktemp "$STATE_DIR/session_prompt.XXXXXX.md")

# --- Resolve active goal/project from Loom and generate the context snapshot ---
# Moved up (was previously generated after prompt assembly): the dynamic goal
# text built below depends on this snapshot existing first. Single source of
# truth for active-goal resolution (priority DESC, id ASC among
# scheduled/in_progress) — was previously a local duplicated SQL query here,
# and a second, broken copy in interactive.sh (`status='active'`, a value
# that can never be set).
LOOM_CONTEXT_FILE="$STATE_DIR/loom_context.json"
LOOM_SRC="${HOME}/lain/loom"
LOOM_DB="${LOOM_DB:-${HOME}/.local/share/loom/loom.db}"
export LOOM_DB
LOOM_SESSION_ROW_ID=""
ACTIVE_GOAL_ID=""
if [ -d "$LOOM_SRC" ] && [ -f "$LOOM_SRC/.venv/bin/python" ]; then
  loom_py() { PYTHONPATH="$LOOM_SRC" "$LOOM_SRC/.venv/bin/python" -m loom.cli --db "$LOOM_DB" "$@"; }

  ACTIVE_GOAL_ID=$(loom_py goal resolve-active 2>/dev/null || echo "")
  GOAL_ARG=""
  if [ -n "$ACTIVE_GOAL_ID" ]; then
    GOAL_ARG="--goal $ACTIVE_GOAL_ID"
    log_line "LOOM active goal detected: ID=$ACTIVE_GOAL_ID"
  fi

  loom_py context $GOAL_ARG --output "$LOOM_CONTEXT_FILE" > /dev/null 2>&1 \
    && log_line "LOOM context snapshot written to $LOOM_CONTEXT_FILE" \
    || log_line "WARNING: loom context snapshot failed (non-fatal)."

  # Record session start in loom_sessions table.
  LOOM_SESSION_ROW_ID=$(loom_py session start \
    --date "$night_id" --number "$new_count" \
    --type "$TRIGGER_MODE" ${ACTIVE_GOAL_ID:+--goal "$ACTIVE_GOAL_ID"} 2>/dev/null || echo "")
  if [ -n "$LOOM_SESSION_ROW_ID" ]; then
    log_line "LOOM session row created: id=$LOOM_SESSION_ROW_ID"
    # Write row ID to state file so the agent can update handoff note during shutdown.
    echo "$LOOM_SESSION_ROW_ID" > "$STATE_DIR/current_loom_session_id.txt"
  else
    log_line "WARNING: loom session start failed (non-fatal)."
    rm -f "$STATE_DIR/current_loom_session_id.txt"
  fi
fi

# --- Build the dynamic goal text (replaces the old static goal.txt) ---
# Sourced from the context snapshot just written: active goal/project name +
# description. Empty/no active goal is not an error — the default-goal
# fallback (resolve_session_type.py Priority 4) and philosophy framing carry
# that case, same as an empty goal.txt used to fall through before.
GOAL_FILE=$(mktemp "$STATE_DIR/goal_text.XXXXXX.md")
DYNAMIC_GOAL_FILE="$GOAL_FILE"
python3 -c "
import json
try:
    d = json.load(open('$LOOM_CONTEXT_FILE'))
except Exception:
    d = {}
g = d.get('active_goal')
p = d.get('active_project')
lines = []
if g:
    lines.append(f\"Active goal: {g.get('name')}\")
    if g.get('description'):
        lines.append(g['description'])
else:
    lines.append('No active goal currently set.')
if p:
    lines.append(f\"Active project: {p.get('name')}\")
    if p.get('description'):
        lines.append(p['description'])
open('$GOAL_FILE', 'w').write('\n\n'.join(lines) + '\n')
" 2>/dev/null || echo "No active goal currently set." > "$GOAL_FILE"
log_line "Dynamic goal text built from Loom (active_goal_id=${ACTIVE_GOAL_ID:-none})."

if [ -n "$PERSONA_FILE" ] && [ -f "$PERSONA_FILE" ]; then
  persona_arg="$PERSONA_FILE"
else
  persona_arg=""
fi

# --- Session type resolution ---
# Priority: SESSION_TYPE env var → Loom queue state → default (maintenance)
# Resolves type, loads type config YAML, assembles context files + type prompt.
SESSION_TYPE_RESULT=$(mktemp "$STATE_DIR/session_type_result.XXXXXX.json")
if [ -f "$PROJECT_DIR/scripts/executional/resolve_session_type.py" ]; then
  _type_stderr=$(python3 "$PROJECT_DIR/scripts/executional/resolve_session_type.py" \
    --project-dir "$PROJECT_DIR" \
    --trigger-mode "$TRIGGER_MODE" \
    --output "$SESSION_TYPE_RESULT" 2>&1) || true
  log_line "Session type resolution: ${_type_stderr:-no output}"

  # Parse resolved type and resolution source.
  CURRENT_SESSION_TYPE=$(python3 -c \
    "import json,sys; d=json.load(open(sys.argv[1])); print(d.get('session_type','execution'))" \
    "$SESSION_TYPE_RESULT" 2>/dev/null || echo "execution")
  CURRENT_SESSION_TYPE_SOURCE=$(python3 -c \
    "import json,sys; d=json.load(open(sys.argv[1])); print(d.get('resolution_source','default'))" \
    "$SESSION_TYPE_RESULT" 2>/dev/null || echo "default")

  # Export maintenance scope if present (scope_id/scope_slug from resolve_session_type.py result).
  MAINTENANCE_SCOPE=$(python3 -c \
    "import json,sys; d=json.load(open(sys.argv[1])); s=d.get('scope_id'); print(s if s else '')" \
    "$SESSION_TYPE_RESULT" 2>/dev/null || echo "")
  MAINTENANCE_SCOPE_SLUG=$(python3 -c \
    "import json,sys; d=json.load(open(sys.argv[1])); s=d.get('scope_slug'); print(s if s else '')" \
    "$SESSION_TYPE_RESULT" 2>/dev/null || echo "")
  export MAINTENANCE_SCOPE

  # Qualify maintenance's logged/reported type by scope (e.g. maintenance_memory)
  # so session logs, session history, and analytics distinguish which of the 3
  # rotating scopes ran -- previously every maintenance session was logged
  # identically as "maintenance" regardless of scope. Config-file lookup above
  # already used the base "maintenance" identity; this only affects what gets
  # reported downstream from here.
  if [ "$CURRENT_SESSION_TYPE" = "maintenance" ] && [ -n "$MAINTENANCE_SCOPE_SLUG" ]; then
    CURRENT_SESSION_TYPE="maintenance_${MAINTENANCE_SCOPE_SLUG}"
  fi

  export CURRENT_SESSION_TYPE CURRENT_SESSION_TYPE_SOURCE
  echo "$CURRENT_SESSION_TYPE" > "$STATE_DIR/current_session_type.txt"
  log_line "Session type: $CURRENT_SESSION_TYPE (source: $CURRENT_SESSION_TYPE_SOURCE)"
  [ -n "$MAINTENANCE_SCOPE" ] && log_line "Maintenance scope: $MAINTENANCE_SCOPE ($MAINTENANCE_SCOPE_SLUG)"

  # Build augmented goal: type prompt + context preload + original goal content.
  # Falls back to original goal if the augmentation step fails.
  AUGMENTED_GOAL=$(mktemp "$STATE_DIR/augmented_goal.XXXXXX.md")
  python3 -c "
import json, sys
try:
    data = json.load(open(sys.argv[1]))
    goal = open(sys.argv[2]).read().strip()
    parts = []
    p = data.get('prompt_content', '').strip()
    c = data.get('assembled_context', '').strip()
    t = data.get('target_context', '').strip()
    # Substitute maintenance scope placeholders if present
    if p and data.get('scope_id'):
        scope_name = data.get('scope_name', f'Scope {data[\"scope_id\"]}')
        focus_hint = data.get('focus_hint', '')
        p = p.replace('{MAINTENANCE_SCOPE_NAME}', scope_name)
        p = p.replace('{MAINTENANCE_SCOPE_FOCUS}', focus_hint)
    if p:
        parts.append(p)
    if c:
        parts.append('## CONTEXT PRELOAD\n\n' + c)
    if t:
        # The specific goal/project record this session's dispatch rule matched
        # (evaluation/planning/audit at goal or project level) — system-wide,
        # not necessarily the same record as state/loom_context.json's active
        # goal/project, since rules 1-3 aren't scoped to whichever goal is active.
        parts.append('## TARGET\n\n' + t)
    parts.append(goal)
    open(sys.argv[3], 'w').write('\n\n---\n\n'.join(parts) + '\n')
except Exception:
    import shutil
    shutil.copy(sys.argv[2], sys.argv[3])
" "$SESSION_TYPE_RESULT" "$GOAL_FILE" "$AUGMENTED_GOAL" 2>/dev/null \
    || cp "$GOAL_FILE" "$AUGMENTED_GOAL"

  rm -f "$SESSION_TYPE_RESULT"

  # Gate: abort if consecutive philosophy cap reached (no Claude launch needed).
  if [ "$CURRENT_SESSION_TYPE" = "philosophy_cap" ]; then
    _consec=$(cat "$STATE_DIR/consecutive_philosophy.count" 2>/dev/null || echo "3")
    log_line "ABORT: philosophy cap reached (consecutive_philosophy.count=$_consec). Skipping session."
    write_trigger_result "aborted" "philosophy cap reached (consecutive_philosophy_count=$_consec)"
    rm -f "$AUGMENTED_GOAL" "$DYNAMIC_GOAL_FILE"
    exit 0
  fi

  GOAL_FILE="$AUGMENTED_GOAL"
  log_line "Augmented goal built for session type: $CURRENT_SESSION_TYPE"
else
  log_line "WARNING: resolve_session_type.py not found — skipping type injection."
  CURRENT_SESSION_TYPE="execution"
  CURRENT_SESSION_TYPE_SOURCE="default"
  export CURRENT_SESSION_TYPE CURRENT_SESSION_TYPE_SOURCE
  echo "$CURRENT_SESSION_TYPE" > "$STATE_DIR/current_session_type.txt"
fi

write_trigger_result "launched" "" "$CURRENT_SESSION_TYPE" "$$"

# --- Inject codebase brief if orchestrator has set an active project path ---
_ACTIVE_PROJECT_PATH_FILE="$STATE_DIR/active_project_path.txt"
if [ -f "$_ACTIVE_PROJECT_PATH_FILE" ]; then
  _active_project_path=$(cat "$_ACTIVE_PROJECT_PATH_FILE")
  _project_name=$(basename "$_active_project_path")
  _brief_file="$PROJECT_DIR/memory/codebase_briefs/${_project_name}.md"
  if [ -f "$_brief_file" ] && grep -q "## Narrative" "$_brief_file" 2>/dev/null; then
    { echo ""; echo "---"; echo ""; echo "## Codebase Brief: $_project_name"; echo ""; cat "$_brief_file"; } >> "$GOAL_FILE"
    log_line "Injected codebase brief for $_project_name into goal context."
  fi
  unset _active_project_path _project_name _brief_file
fi
unset _ACTIVE_PROJECT_PATH_FILE

python3 "$PROJECT_DIR/scripts/executional/splice_prompt.py" \
  "$WRAPPER_TEMPLATE" "$GOAL_FILE" "$session_prompt" "$persona_arg"

# Record count BEFORE launching — counts even if agent crashes or hangs.
echo "$new_count" > "$COUNT_FILE"

# Update mode-specific counters (manual / emergency).
# COUNT_FILE always tracks sessions_tonight.count; analytics_write.py::derive_session_key()
# reads mode-specific files, so they must be kept in sync.
case "$TRIGGER_MODE" in
  manual)
    _mc="$STATE_DIR/sessions_manual.count"
    _mv=$(( $(cat "$_mc" 2>/dev/null || echo "0") + 1 ))
    echo "$_mv" > "$_mc"
    unset _mc _mv
    ;;
  emergency)
    _mc="$STATE_DIR/sessions_emergency.count"
    _mv=$(( $(cat "$_mc" 2>/dev/null || echo "0") + 1 ))
    echo "$_mv" > "$_mc"
    unset _mc _mv
    ;;
esac

# Write the canonical session key (night_id + count) so analytics_write.py
# can look it up directly instead of re-deriving it with potentially stale files.
echo "${night_id}_${new_count}" > "$STATE_DIR/current_session_key.txt"

# Write trigger mode so Lain can read it during orientation.
echo "$TRIGGER_MODE" > "$STATE_DIR/trigger_mode.txt"

session_start_epoch=$(date +%s)
echo "$session_start_epoch" > "$STATE_DIR/session_start_epoch"

# Write lock file. EXIT trap ensures cleanup even on crash.
echo $$ > "$LOCK_FILE"
trap 'rm -f "$LOCK_FILE"' EXIT

# --- Nexus heartbeat: push last_seen at session start (non-fatal, T402) ---
_nexus_token_file="$STATE_DIR/nexus_${AGENT_NAME}_token.txt"
if [ -f "$_nexus_token_file" ]; then
  _hb_token=$(cat "$_nexus_token_file")
  curl -s --max-time 5 -X POST "$NEXUS_URL/agents/${AGENT_NAME}/heartbeat" \
    -H "Authorization: Bearer $_hb_token" \
    -H "Content-Type: application/json" \
    -d "{\"status\":\"session_start\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" \
    > /dev/null 2>&1 || true
  log_line "Nexus heartbeat sent (non-fatal if endpoint not yet live)."
fi

# --- Generate behavioral context snapshot (non-fatal) ---
BEHAVIORAL_TOOL="$PROJECT_DIR/tools/executional/behavioral_adapter.py"
BEHAVIORAL_PROFILE="$PROJECT_DIR/memory/work/musubi_data/users/${AGENT_NAME}/${OWNER_NAME}.md"
BEHAVIORAL_CONTEXT="$STATE_DIR/behavioral_context.txt"
if [ -f "$BEHAVIORAL_TOOL" ] && [ -f "$BEHAVIORAL_PROFILE" ]; then
  /usr/bin/python3 "$BEHAVIORAL_TOOL" \
    --user-file "$BEHAVIORAL_PROFILE" \
    --output "$BEHAVIORAL_CONTEXT" > /dev/null 2>&1 \
    && log_line "Behavioral context generated: $BEHAVIORAL_CONTEXT" \
    || log_line "WARNING: behavioral_adapter.py failed (non-fatal)."
fi

# --- Poll argus for fresh owner context snapshot (non-fatal) ---
ARGUS_POLLER="$PROJECT_DIR/tools/executional/argus_context_poller.py"
ARGUS_CONTEXT="$STATE_DIR/argus_context.json"
if [ -f "$ARGUS_POLLER" ] && grep -q "^ARGUS_URL=" "$STATE_DIR/agent_config.env" 2>/dev/null; then
  /usr/bin/python3 "$ARGUS_POLLER" > /dev/null 2>&1 || true
  ARGUS_STATE=$(/usr/bin/python3 -c \
    "import json; d=json.load(open('$ARGUS_CONTEXT')); print(d.get('state','unknown'))" \
    2>/dev/null || echo "unknown")
  export ARGUS_STATE
  log_line "argus_state: $ARGUS_STATE"
fi

# --- Prune processed inbox entries older than 7 days (non-fatal) ---
INBOX_TOOL="$PROJECT_DIR/tools/inbox.py"
if [ -f "$INBOX_TOOL" ]; then
  _prune_result=$(/usr/bin/python3 "$INBOX_TOOL" prune 2>/dev/null || echo "")
  [ -n "$_prune_result" ] && log_line "$_prune_result" || true
fi

# --- Launch Claude Code headless ---
# --dangerously-skip-permissions is intentional: agent runs unattended.
# Containment is handled at the VM/network level, not here.
SESSION_MODEL_FILE="$STATE_DIR/session_model.txt"
if [[ -f "$SESSION_MODEL_FILE" ]] && [[ -s "$SESSION_MODEL_FILE" ]]; then
  SESSION_MODEL=$(cat "$SESSION_MODEL_FILE")
else
  SESSION_MODEL="${NODE_VERSION:-claude-sonnet-4-6}"
fi
log_line "Using model: $SESSION_MODEL"

# set +e around the launch: a crashed/killed/non-zero-exiting session must NOT
# abort wake.sh here under `set -e`, or all the post-session bookkeeping below
# (Loom session end, relationship update, consecutive-philosophy counter,
# analytics fallback) gets silently skipped for exactly the sessions — crashes —
# where you'd most want a record.
set +e
claude --dangerously-skip-permissions --model "$SESSION_MODEL" < "$session_prompt" \
  >> "$LOG_DIR/session_${night_id}_${new_count}.out" \
  2>> "$LOG_DIR/session_${night_id}_${new_count}.err"
exit_code=$?
set -e
session_end_epoch=$(date +%s)
duration_min=$(( (session_end_epoch - session_start_epoch) / 60 ))

log_line "Session #$new_count ended. exit_code=$exit_code duration_min=$duration_min"
rm -f "$session_prompt"
[ -n "${AUGMENTED_GOAL:-}" ] && rm -f "$AUGMENTED_GOAL"
rm -f "$DYNAMIC_GOAL_FILE"

# --- Update consecutive philosophy counter ---
# The three escalation-ladder types increment; any other type resets to 0.
# philosophy_cap never reaches here -- it exits before Claude ever launches.
_CONSEC_FILE="$STATE_DIR/consecutive_philosophy.count"
case "${CURRENT_SESSION_TYPE:-}" in
  philosophy|creative|blocker_resolver)
    _prev=$(cat "$_CONSEC_FILE" 2>/dev/null || echo "0")
    _next=$(( _prev + 1 ))
    echo "$_next" > "$_CONSEC_FILE"
    log_line "consecutive_philosophy.count: $_prev -> $_next"
    ;;
  *)
    echo "0" > "$_CONSEC_FILE"
    log_line "consecutive_philosophy.count reset to 0 (session_type=${CURRENT_SESSION_TYPE:-unknown})"
    ;;
esac
unset _CONSEC_FILE _prev _next

# --- Record session end in Loom (non-fatal) ---
if [ -n "$LOOM_SESSION_ROW_ID" ] && [ -d "$LOOM_SRC" ] && [ -f "$LOOM_SRC/.venv/bin/python" ]; then
  EXIT_REASON="exit_code=$exit_code"
  PYTHONPATH="$LOOM_SRC" "$LOOM_SRC/.venv/bin/python" -m loom.cli --db "$LOOM_DB" \
    session end --id "$LOOM_SESSION_ROW_ID" \
    --exit-reason "$EXIT_REASON" > /dev/null 2>&1 \
    && log_line "LOOM session $LOOM_SESSION_ROW_ID closed." \
    || log_line "WARNING: loom session end failed (non-fatal)."
fi

# --- Update relationship state (non-fatal, heuristic mode) ---
# Reads last 60 lines of wake.log for this session as classification context.
# Applies decay + classifies events → updates memory/work/musubi_data/users/lain/andrii.md
REL_TOOL="$PROJECT_DIR/tools/executional/relationship_update.py"
REL_PROFILE="$PROJECT_DIR/memory/work/musubi_data/users/${AGENT_NAME}/${OWNER_NAME}.md"
if [ -f "$REL_TOOL" ] && [ -f "$REL_PROFILE" ]; then
  tail -60 "$LOG_DIR/wake.log" | /usr/bin/python3 "$REL_TOOL" \
    --user-file "$REL_PROFILE" \
    --heuristic --stdin > /dev/null 2>&1 \
    && log_line "Relationship state updated." \
    || log_line "WARNING: relationship_update.py failed (non-fatal)."
fi

# --- Write analytics record (fallback — agent writes it during shutdown; this catches gate aborts) ---
# --skip-if-exists: if agent already wrote it during its own shutdown, preserve the richer record.
ANALYTICS_TOOL="$PROJECT_DIR/tools/executional/analytics_write.py"
if [ -f "$ANALYTICS_TOOL" ]; then
  CONTEXT_PCT_AT_EXIT=$(bash "$PROJECT_DIR/tools/executional/check_session.sh" --context 2>/dev/null \
    | grep "context_pct_estimate" | grep -oP '\d+(?=%)' | head -1 || echo "0")
  /usr/bin/python3 "$ANALYTICS_TOOL" \
    --session-type "${CURRENT_SESSION_TYPE:-execution}" \
    --exit-reason "gate_abort" \
    --summary "wake.sh fallback — agent did not write analytics" \
    --tasks-completed 0 \
    --context-pct "${CONTEXT_PCT_AT_EXIT:-0}" \
    --no-transcript \
    --skip-if-exists \
    > /dev/null 2>&1 \
    && log_line "Analytics fallback record written (or skipped — agent already wrote it)." \
    || log_line "WARNING: analytics_write.py fallback failed (non-fatal)."
fi
