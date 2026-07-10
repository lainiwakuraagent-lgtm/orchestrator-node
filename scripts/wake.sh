#!/usr/bin/env bash
# wake.sh
# Invoked by systemd timers or the manual trigger server.
# Supports three launch modes via TRIGGER_MODE env var:
#   nightly   (default) — scheduled night sessions, full gate enforcement
#   emergency           — emergency/daytime mode, time+count gates bypassed
#   manual              — owner-initiated trigger, time+count gates bypassed
#
# Usage: wake.sh <goal_file> [persona_file]

set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/home/andrii/lain/agent_project}"
STATE_DIR="$PROJECT_DIR/state"
LOG_DIR="$PROJECT_DIR/logs"
WRAPPER_TEMPLATE="$PROJECT_DIR/prompts/wrapper_prompt.md"
DATE_FILE="$STATE_DIR/sessions_tonight.date"
MAX_SESSIONS_PER_NIGHT=5
EMERGENCY_FLAG="$STATE_DIR/emergency_mode.active"

# --- Load agent config (parameterize for new node instances) ---
AGENT_CONFIG="$STATE_DIR/agent_config.env"
if [ -f "$AGENT_CONFIG" ]; then
  # shellcheck disable=SC1090
  source "$AGENT_CONFIG"
fi
AGENT_NAME="${AGENT_NAME:-lain}"
OWNER_NAME="${OWNER_NAME:-andrii}"
AGENT_REPO="${AGENT_REPO:-lainiwakuraagent-lgtm/node}"
NODE_VERSION="${NODE_VERSION:-claude-sonnet-4-6}"
NEXUS_URL="${NEXUS_URL:-http://100.110.36.84:8900}"

GOAL_FILE="${1:?Usage: wake.sh <goal_file> [persona_file]}"
PERSONA_FILE="${2:-}"

mkdir -p "$STATE_DIR" "$LOG_DIR"

timestamp() { date '+%Y-%m-%d %H:%M:%S %Z'; }
log_line() { echo "[$(timestamp)] $*" >> "$LOG_DIR/wake.log"; }

# --- Determine trigger mode and select matching counter file ---
TRIGGER_MODE="${TRIGGER_MODE:-nightly}"
case "$TRIGGER_MODE" in
  nightly)   COUNT_FILE="$STATE_DIR/sessions_tonight.count" ;;
  emergency) COUNT_FILE="$STATE_DIR/sessions_emergency.count" ;;
  manual)    COUNT_FILE="$STATE_DIR/sessions_manual.count" ;;
  *)
    log_line "ERROR: unknown TRIGGER_MODE '$TRIGGER_MODE'. Defaulting to nightly."
    TRIGGER_MODE="nightly"
    COUNT_FILE="$STATE_DIR/sessions_tonight.count"
    ;;
esac

log_line "Wake called. TRIGGER_MODE=$TRIGGER_MODE"

# --- Nightly only: compute night ID and reset counter on new night ---
if [ "$TRIGGER_MODE" = "nightly" ]; then
  hour=$(date +%H); hour=$((10#$hour))
  if [ "$hour" -lt 6 ]; then
    night_id=$(date -d "yesterday" +%Y-%m-%d)
  else
    night_id=$(date +%Y-%m-%d)
  fi

  last_recorded_night=""
  if [ -f "$DATE_FILE" ]; then
    last_recorded_night=$(cat "$DATE_FILE")
  fi
  if [ "$last_recorded_night" != "$night_id" ]; then
    echo "0" > "$COUNT_FILE"
    echo "$night_id" > "$DATE_FILE"
    log_line "New night detected ($night_id). Session counter reset to 0."
  fi
else
  night_id=$(date +%Y-%m-%d)
fi

current_count=$(cat "$COUNT_FILE" 2>/dev/null || echo "0")

# --- Gate 0: subscription usage limits (all modes) ---
# Fail-open on errors so network/auth issues never silently kill the launch.
usage_check_output=$(bash "$PROJECT_DIR/tools/check_usage.sh" 2>&1) \
  || usage_check_output="ACTION: cannot check usage -- treat as unknown, proceed with caution."
usage_action=$(echo "$usage_check_output" | grep '^ACTION:' | head -n1)

if echo "$usage_action" | grep -q 'usage limit exceeded'; then
  log_line "ABORT: subscription usage too high. check_usage.sh output: $usage_check_output"
  exit 0
fi
if echo "$usage_action" | grep -q 'cannot check usage'; then
  log_line "WARNING: could not check usage limits (proceeding). check_usage.sh output: $usage_check_output"
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

# --- Gate 2 (nightly only): time window 23:00–06:00 ---
if [ "$TRIGGER_MODE" = "nightly" ]; then
  time_check_output=$(bash "$PROJECT_DIR/tools/check_time.sh")
  in_window=$(echo "$time_check_output" | grep '^in_work_window:' | awk '{print $2}')

  if [ "$in_window" != "true" ]; then
    log_line "ABORT: called outside work window. check_time.sh output:"
    log_line "$time_check_output"
    exit 0
  fi
else
  log_line "Gate 2 (time window) skipped — TRIGGER_MODE=$TRIGGER_MODE."
fi

# --- Gate 3 (nightly only): session count hard cap ---
if [ "$TRIGGER_MODE" = "nightly" ]; then
  if [ "$current_count" -ge "$MAX_SESSIONS_PER_NIGHT" ]; then
    log_line "ABORT: already had $current_count session(s) tonight (max $MAX_SESSIONS_PER_NIGHT). Skipping."
    exit 0
  fi
else
  log_line "Gate 3 (session count) informational — TRIGGER_MODE=$TRIGGER_MODE, current count: $current_count."
fi

# --- Gate 4: no session already running (all modes) ---
LOCK_FILE="$STATE_DIR/session.lock"
if [ -f "$LOCK_FILE" ]; then
  locked_pid=$(cat "$LOCK_FILE" 2>/dev/null || echo "")
  if [ -n "$locked_pid" ] && kill -0 "$locked_pid" 2>/dev/null; then
    log_line "SKIP: session already running (PID $locked_pid). Skipping this wake."
    exit 0
  else
    log_line "WARNING: stale lock found (PID ${locked_pid:-unknown} is dead). Removing and proceeding."
    rm -f "$LOCK_FILE"
  fi
fi

# --- All gates passed: launch the agent ---
new_count=$((current_count + 1))
log_line "LAUNCHING session #$new_count (mode=$TRIGGER_MODE, night=$night_id). Goal file: $GOAL_FILE"

# Build prompt by splicing goal (and optional persona) into the wrapper template.
session_prompt=$(mktemp "$STATE_DIR/session_prompt.XXXXXX.md")

# If goal.txt has "GOAL_STATUS: complete" on line 1, fall back to default_goal.txt.
GOAL_STATUS=$(awk 'NR==1 && /GOAL_STATUS:/ {print $2; exit}' "$GOAL_FILE")
if [ "$GOAL_STATUS" = "complete" ]; then
  DEFAULT_GOAL="$PROJECT_DIR/prompts/default_goal.txt"
  if [ -f "$DEFAULT_GOAL" ]; then
    log_line "Goal marked complete. Using default_goal.txt for this session."
    GOAL_FILE="$DEFAULT_GOAL"
  else
    log_line "WARNING: goal marked complete but default_goal.txt not found. Using original goal file."
  fi
fi

if [ -n "$PERSONA_FILE" ] && [ -f "$PERSONA_FILE" ]; then
  persona_arg="$PERSONA_FILE"
else
  persona_arg=""
fi

python3 "$PROJECT_DIR/scripts/splice_prompt.py" \
  "$WRAPPER_TEMPLATE" "$GOAL_FILE" "$session_prompt" "$persona_arg"

# --- Generate LOOM context snapshot and record session start (optional, non-fatal) ---
LOOM_CONTEXT_FILE="$STATE_DIR/loom_context.json"
LOOM_SRC="${HOME}/lain/loom"
LOOM_DB="${HOME}/.local/share/loom/loom.db"
LOOM_SESSION_ROW_ID=""
if [ -d "$LOOM_SRC" ] && [ -f "$LOOM_SRC/.venv/bin/python" ]; then
  loom_py() { PYTHONPATH="$LOOM_SRC" "$LOOM_SRC/.venv/bin/python" -m loom.cli --db "$LOOM_DB" "$@"; }

  # Detect active goal from DB (python3 — sqlite3 CLI not available on this machine).
  ACTIVE_GOAL_ID=$(python3 -c \
    "import sqlite3,sys; c=sqlite3.connect('$LOOM_DB'); r=c.execute(\"SELECT id FROM goals WHERE status='active' LIMIT 1\").fetchone(); print(r[0] if r else '')" \
    2>/dev/null || echo "")
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

# Record count BEFORE launching — counts even if agent crashes or hangs.
echo "$new_count" > "$COUNT_FILE"

# For nightly: write max so the wrapper prompt can compare count vs max.
# For emergency/manual: no max file — count is informational only.
if [ "$TRIGGER_MODE" = "nightly" ]; then
  echo "$MAX_SESSIONS_PER_NIGHT" > "$STATE_DIR/sessions_tonight.max"
fi

# Write trigger mode so Lain can read it during orientation.
echo "$TRIGGER_MODE" > "$STATE_DIR/trigger_mode.txt"

session_start_epoch=$(date +%s)
echo "$session_start_epoch" > "$STATE_DIR/session_start_epoch"

# Write lock file. EXIT trap ensures cleanup even on crash.
echo $$ > "$LOCK_FILE"
trap 'rm -f "$LOCK_FILE"' EXIT

# --- Refresh Nexus JWT token (non-fatal) ---
# Keeps state/nexus_lain_token.txt fresh so Lain can use it immediately each session.
NEXUS_PASS_FILE="$PROJECT_DIR/identity/nexus_seed_passwords.txt"
if [ -f "$NEXUS_PASS_FILE" ]; then
  _nexus_pass=$(grep "^# ${AGENT_NAME}" "$NEXUS_PASS_FILE" | grep -o '[^ ]*$' | head -1)
  _nexus_token=$(curl -s --max-time 5 -X POST "${NEXUS_URL}/auth/token" \
    -H "Content-Type: application/json" \
    -d "{\"username\":\"${AGENT_NAME}\",\"password\":\"$_nexus_pass\"}" \
    | /usr/bin/python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('access_token',''))" \
    2>/dev/null || echo "")
  if [ -n "$_nexus_token" ]; then
    echo "$_nexus_token" > "$STATE_DIR/nexus_${AGENT_NAME}_token.txt"
    log_line "Nexus JWT refreshed."
  else
    log_line "WARNING: Nexus JWT refresh failed (non-fatal) — nexus may be down or password changed."
  fi
fi

# --- Generate behavioral context snapshot (non-fatal) ---
BEHAVIORAL_TOOL="$PROJECT_DIR/tools/behavioral_adapter.py"
BEHAVIORAL_PROFILE="$PROJECT_DIR/memory/work/musubi_data/users/${AGENT_NAME}/${OWNER_NAME}.md"
BEHAVIORAL_CONTEXT="$STATE_DIR/behavioral_context.txt"
if [ -f "$BEHAVIORAL_TOOL" ] && [ -f "$BEHAVIORAL_PROFILE" ]; then
  /usr/bin/python3 "$BEHAVIORAL_TOOL" \
    --user-file "$BEHAVIORAL_PROFILE" \
    --output "$BEHAVIORAL_CONTEXT" > /dev/null 2>&1 \
    && log_line "Behavioral context generated: $BEHAVIORAL_CONTEXT" \
    || log_line "WARNING: behavioral_adapter.py failed (non-fatal)."
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

claude --dangerously-skip-permissions --model "$SESSION_MODEL" < "$session_prompt" \
  >> "$LOG_DIR/session_${night_id}_${new_count}.out" \
  2>> "$LOG_DIR/session_${night_id}_${new_count}.err"

exit_code=$?
session_end_epoch=$(date +%s)
duration_min=$(( (session_end_epoch - session_start_epoch) / 60 ))

log_line "Session #$new_count ended. exit_code=$exit_code duration_min=$duration_min"
rm -f "$session_prompt"

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
REL_TOOL="$PROJECT_DIR/tools/relationship_update.py"
REL_PROFILE="$PROJECT_DIR/memory/work/musubi_data/users/${AGENT_NAME}/${OWNER_NAME}.md"
if [ -f "$REL_TOOL" ] && [ -f "$REL_PROFILE" ]; then
  tail -60 "$LOG_DIR/wake.log" | /usr/bin/python3 "$REL_TOOL" \
    --user-file "$REL_PROFILE" \
    --heuristic --stdin --nexus-notify > /dev/null 2>&1 \
    && log_line "Relationship state updated + broadcast to Nexus quorum-ops." \
    || log_line "WARNING: relationship_update.py failed (non-fatal)."
fi
