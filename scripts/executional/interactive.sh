#!/usr/bin/env bash
# interactive.sh — Launch an interactive Claude Code session with full agent context.
#
# Usage (from project root or any directory):
#   bash scripts/interactive.sh [--project /path/to/side/project]
#
# Options:
#   --project <path>   Work on a side project instead of the agent's own project.
#                      Generates a codebase brief, then launches Claude with
#                      the side project as the working directory. Agent memory
#                      (state/, memory/) remains accessible via --add-dir.
#
# What it does:
#   1. Refreshes Loom context snapshot
#   2. Refreshes behavioral context (tone calibration)
#   3. Refreshes Nexus JWT token
#   4. Writes trigger_mode=manual to state/
#   5. Launches claude interactively (no dangerously-skip-permissions — owner is present)
#
# The agent's CLAUDE.md in the project root tells Claude Code who it is
# and what files to read at session start.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$PROJECT_DIR"

# --- Parse arguments ---
SIDE_PROJECT=""
while [[ $# -gt 0 ]]; do
  case $1 in
    --project)
      SIDE_PROJECT="$2"
      shift 2
      ;;
    *)
      shift
      ;;
  esac
done

# --- Load agent config ---
AGENT_CONFIG="state/agent_config.env"
if [ -f "$AGENT_CONFIG" ]; then
  # shellcheck disable=SC1090
  source "$AGENT_CONFIG"
fi
AGENT_NAME="${AGENT_NAME:-agent}"
OWNER_NAME="${OWNER_NAME:-andrii}"
NODE_VERSION="${NODE_VERSION:-claude-sonnet-4-6}"
NEXUS_URL="${NEXUS_URL:-http://100.110.36.84:8900}"
LOOM_SRC="${HOME}/lain/loom"
LOOM_DB="${HOME}/.local/share/loom/loom.db"

echo ""
echo "=== ${AGENT_NAME} interactive session ==="
echo "Model  : ${NODE_VERSION}"
echo "Project: ${PROJECT_DIR}"
echo ""

# --- Refresh Loom context snapshot (non-fatal) ---
if [ -d "$LOOM_SRC" ] && [ -f "$LOOM_SRC/.venv/bin/python" ]; then
  loom_py() { PYTHONPATH="$LOOM_SRC" "$LOOM_SRC/.venv/bin/python" -m loom.cli --db "$LOOM_DB" "$@"; }

  # Single source of truth for active-goal resolution (priority DESC, id ASC
  # among scheduled/in_progress) — was previously a local `status='active'`
  # query here that could never match, since that status value doesn't exist.
  ACTIVE_GOAL_ID=$(loom_py goal resolve-active 2>/dev/null || echo "")
  GOAL_ARG=""
  if [ -n "$ACTIVE_GOAL_ID" ]; then
    GOAL_ARG="--goal $ACTIVE_GOAL_ID"
  fi
  loom_py context $GOAL_ARG --output "state/loom_context.json" > /dev/null 2>&1 \
    && echo "[ok] Loom context refreshed (goal=${ACTIVE_GOAL_ID:-none})" \
    || echo "[warn] Loom context refresh failed (continuing)"
fi

# --- Refresh behavioral context (non-fatal) ---
BEHAVIORAL_PROFILE="memory/work/musubi_data/users/${AGENT_NAME}/${OWNER_NAME}.md"
if [ -f "tools/executional/behavioral_adapter.py" ] && [ -f "$BEHAVIORAL_PROFILE" ]; then
  /usr/bin/python3 tools/executional/behavioral_adapter.py \
    --user-file "$BEHAVIORAL_PROFILE" \
    --output state/behavioral_context.txt > /dev/null 2>&1 \
    && echo "[ok] Behavioral context refreshed" \
    || echo "[warn] Behavioral context refresh failed (continuing)"
fi

# --- Refresh Nexus JWT (non-fatal) ---
NEXUS_PASS_FILE="identity/nexus_seed_passwords.txt"
if [ -f "$NEXUS_PASS_FILE" ]; then
  _nexus_pass=$(grep "^# ${AGENT_NAME}" "$NEXUS_PASS_FILE" 2>/dev/null | grep -o '[^ ]*$' | head -1 || echo "")
  if [ -n "$_nexus_pass" ]; then
    _nexus_token=$(curl -s --max-time 5 -X POST "${NEXUS_URL}/auth/token" \
      -H "Content-Type: application/json" \
      -d "{\"username\":\"${AGENT_NAME}\",\"password\":\"${_nexus_pass}\"}" \
      | /usr/bin/python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('access_token',''))" \
      2>/dev/null || echo "")
    if [ -n "$_nexus_token" ]; then
      echo "$_nexus_token" > "state/nexus_${AGENT_NAME}_token.txt"
      echo "[ok] Nexus JWT refreshed"
    else
      echo "[warn] Nexus JWT refresh failed (continuing)"
    fi
  fi
fi

# --- Write trigger mode ---
echo "manual" > state/trigger_mode.txt

# --- Side project setup ---
if [ -n "$SIDE_PROJECT" ]; then
  SIDE_PROJECT="$(realpath "$SIDE_PROJECT")"
  if [ ! -d "$SIDE_PROJECT" ]; then
    echo "[error] --project path does not exist: $SIDE_PROJECT"
    exit 1
  fi
  SIDE_PROJECT_NAME="$(basename "$SIDE_PROJECT")"

  echo ""
  echo "=== Side project: ${SIDE_PROJECT_NAME} ==="
  echo "Path   : ${SIDE_PROJECT}"
  echo ""

  # Generate/refresh codebase brief (non-fatal)
  BRIEF_DIR="${PROJECT_DIR}/memory/architecture/codebase_briefs"
  mkdir -p "$BRIEF_DIR"
  /usr/bin/python3 "${PROJECT_DIR}/tools/executional/codebase_indexer.py" "$SIDE_PROJECT" \
    --output "${BRIEF_DIR}/${SIDE_PROJECT_NAME}.md" > /dev/null 2>&1 \
    && echo "[ok] Codebase brief: memory/architecture/codebase_briefs/${SIDE_PROJECT_NAME}.md" \
    || echo "[warn] Codebase brief generation failed (continuing)"

  # Track active project path (cleared on exit)
  echo "$SIDE_PROJECT" > "${PROJECT_DIR}/state/active_project_path.txt"
  # shellcheck disable=SC2064
  trap "rm -f '${PROJECT_DIR}/state/active_project_path.txt'" EXIT

  # Change to side project directory so Claude's cwd and CLAUDE.md discovery
  # point at the right place.
  cd "$SIDE_PROJECT"

  echo ""
  echo "Launching in: ${SIDE_PROJECT}"
  echo "(agent project accessible via --add-dir for memory/state writes)"
  echo ""

  # Persona injected via --append-system-prompt so identity carries over
  # even though the agent project's CLAUDE.md is not in the new cwd.
  PERSONA_FILE="${PROJECT_DIR}/prompts/persona.txt"
  PERSONA_CONTENT=""
  if [ -f "$PERSONA_FILE" ]; then
    PERSONA_CONTENT="$(cat "$PERSONA_FILE")"
  fi

  if [ -n "$PERSONA_CONTENT" ]; then
    exec claude --model "${NODE_VERSION}" \
      --add-dir "${PROJECT_DIR}" \
      --append-system-prompt "${PERSONA_CONTENT}"
  else
    exec claude --model "${NODE_VERSION}" \
      --add-dir "${PROJECT_DIR}"
  fi
fi

echo ""
echo "Launching... (type /exit or Ctrl+C to quit)"
echo ""

# --- Launch Claude Code interactively ---
# No --dangerously-skip-permissions: the owner is present and can approve tool calls.
# Claude Code reads CLAUDE.md from this directory on startup for agent context.
claude --model "${NODE_VERSION}"
