#!/usr/bin/env bash
# health_check.sh
# Single-script sanity check for the night-agent infrastructure.
# Prints OK / WARN / FAIL for each critical item, plus a summary.
# Run this first when troubleshooting any session startup issue.
#
# Usage: bash tools/health_check.sh
# Exit code: 0 if all OK/WARN, 1 if any FAIL.

set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"

PASS=0
WARN=0
FAIL=0

ok()   { echo "  [OK]   $*";   PASS=$((PASS+1)); }
warn() { echo "  [WARN] $*";   WARN=$((WARN+1)); }
fail() { echo "  [FAIL] $*";   FAIL=$((FAIL+1)); }

# ─── 1. Core directories ──────────────────────────────────────────────────────
echo ""
echo "=== Directories ==="

for d in \
  "$PROJECT_DIR" \
  "$PROJECT_DIR/tools" \
  "$PROJECT_DIR/scripts" \
  "$PROJECT_DIR/prompts" \
  "$PROJECT_DIR/state" \
  "$PROJECT_DIR/logs" \
  "$PROJECT_DIR/memory" \
  "$PROJECT_DIR/memory/sessions" \
  "$PROJECT_DIR/memory/work"
do
  if [ -d "$d" ]; then
    ok "$d"
  else
    fail "missing directory: $d"
  fi
done

# ─── 2. Critical files ────────────────────────────────────────────────────────
echo ""
echo "=== Critical files ==="

for f in \
  "$PROJECT_DIR/tools/executional/check_session.sh" \
  "$PROJECT_DIR/tools/executional/health_check.sh" \
  "$PROJECT_DIR/scripts/executional/wake.sh" \
  "$PROJECT_DIR/scripts/executional/splice_prompt.py" \
  "$PROJECT_DIR/prompts/wrapper_prompt.md"
do
  if [ -f "$f" ]; then
    ok "$f"
  else
    fail "missing file: $f"
  fi
done

# Optional but expected files
# (goal.txt is gone — the active goal is resolved from Loom by wake.sh itself now)
for f in \
  "$PROJECT_DIR/prompts/persona.txt"
do
  if [ -f "$f" ]; then
    ok "$f"
  else
    warn "optional file missing (may be intentional): $f"
  fi
done

# ─── 3. Tool executability ────────────────────────────────────────────────────
echo ""
echo "=== Executability ==="

for f in \
  "$PROJECT_DIR/tools/executional/check_session.sh" \
  "$PROJECT_DIR/tools/executional/health_check.sh" \
  "$PROJECT_DIR/scripts/executional/wake.sh"
do
  if [ -x "$f" ]; then
    ok "executable: $(basename "$f")"
  else
    warn "not executable (run: chmod +x $f): $(basename "$f")"
  fi
done

# ─── 4. Python interpreter ────────────────────────────────────────────────────
echo ""
echo "=== Python ==="

# NOTE: /home/andrii/miniconda3/bin/python does NOT exist on this machine.
# The working interpreter is /usr/bin/python3 (confirmed 2026-06-27).
PY3=""
for candidate in /home/andrii/miniconda3/bin/python /home/andrii/miniconda3/bin/python3 python3 /usr/bin/python3; do
  if command -v "$candidate" &>/dev/null && "$candidate" -c "import json, sys" 2>/dev/null; then
    PY3="$candidate"
    break
  fi
done

if [ -n "$PY3" ]; then
  pyver=$("$PY3" --version 2>&1)
  ok "Python found: $PY3 ($pyver)"
else
  fail "No working Python 3 found — check_session.sh --context/--usage will fail"
fi

if [ "$PY3" != "/home/andrii/miniconda3/bin/python" ] && [ "$PY3" != "" ]; then
  warn "CLAUDE.md says to use /home/andrii/miniconda3/bin/python but it doesn't exist; using $PY3"
fi

# ─── 5. claude CLI ────────────────────────────────────────────────────────────
echo ""
echo "=== Claude CLI ==="

if command -v claude &>/dev/null; then
  claude_path=$(command -v claude)
  ok "claude found at $claude_path"
elif [ "${CI:-}" = "true" ]; then
  warn "claude not on PATH — skipped in CI (not expected in GitHub Actions)"
else
  fail "claude not on PATH — wake.sh cannot launch sessions"
fi

# ─── 6. Credentials ───────────────────────────────────────────────────────────
echo ""
echo "=== Credentials ==="

CREDS_FILE="$CLAUDE_DIR/.credentials.json"
if [ -f "$CREDS_FILE" ]; then
  if [ -r "$CREDS_FILE" ]; then
    # Check it's valid JSON and has expected field
    if python3 -c "import json,sys; d=json.load(open(sys.argv[1])); _ = d['claudeAiOauth']['accessToken']" \
        "$CREDS_FILE" 2>/dev/null; then
      ok "credentials file exists, readable, and has claudeAiOauth.accessToken"
    else
      warn "credentials file exists but may be malformed or missing accessToken field"
    fi
  else
    if [ "${CI:-}" = "true" ]; then
      warn "credentials file not readable — skipped in CI"
    else
      fail "credentials file exists but is not readable: $CREDS_FILE"
    fi
  fi
else
  warn "credentials file not found at $CREDS_FILE — check_session.sh --usage will be unable to probe API"
fi

# ─── 7. State files ───────────────────────────────────────────────────────────
echo ""
echo "=== State ==="

COUNT_FILE="$PROJECT_DIR/state/sessions_tonight.count"
DATE_FILE="$PROJECT_DIR/state/sessions_tonight.date"

if [ -f "$COUNT_FILE" ]; then
  count=$(cat "$COUNT_FILE")
  ok "session count file: $COUNT_FILE (value: $count)"
else
  warn "session count file missing (will be auto-created by wake.sh): $COUNT_FILE"
fi

if [ -f "$DATE_FILE" ]; then
  d=$(cat "$DATE_FILE")
  ok "session date file: $DATE_FILE (value: $d)"
else
  warn "session date file missing (will be auto-created by wake.sh): $DATE_FILE"
fi

# ─── 8. Logs ──────────────────────────────────────────────────────────────────
echo ""
echo "=== Logs ==="

if [ -f "$PROJECT_DIR/logs/wake.log" ]; then
  lines=$(wc -l < "$PROJECT_DIR/logs/wake.log")
  ok "wake.log exists ($lines lines)"
else
  warn "wake.log not yet created (normal for a fresh install)"
fi

SESSION_CSV="$PROJECT_DIR/logs/session_log.csv"
if [ -f "$SESSION_CSV" ]; then
  rows=$(wc -l < "$SESSION_CSV")
  ok "session_log.csv exists ($rows lines)"
else
  warn "session_log.csv not yet created — will be written at end of first session"
fi

# ─── 9. Memory files ──────────────────────────────────────────────────────────
echo ""
echo "=== Memory ==="

for f in \
  "$PROJECT_DIR/memory/progress.md" \
  "$PROJECT_DIR/memory/learnings.md" \
  "$PROJECT_DIR/memory/index.md" \
  "$PROJECT_DIR/memory/latest_summary.md"
do
  if [ -f "$f" ]; then
    ok "$(basename "$f") exists"
  else
    warn "$(basename "$f") not yet created (normal before first session completes)"
  fi
done

# ─── 10. Time window check ────────────────────────────────────────────────────
echo ""
echo "=== Time window ==="

time_out=$(bash "$PROJECT_DIR/tools/executional/check_session.sh" --time 2>/dev/null)
in_window=$(echo "$time_out" | grep '^in_work_window:' | awk '{print $2}')
mins_left=$(echo "$time_out" | grep '^minutes_remaining' | awk '{print $2}')

if [ "${in_window:-false}" = "true" ]; then
  ok "inside work window (${mins_left:-?} minutes remaining)"
else
  warn "outside work window (this is expected when running health check manually)"
fi

# ─── Check: Telegram webhook service ─────────────────────────────────────────
echo ""
echo "=== Telegram webhook ==="

_uid="$(id -u)"
export XDG_RUNTIME_DIR="/run/user/${_uid}"
export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${_uid}/bus"

if systemctl --user is-active telegram-webhook.service >/dev/null 2>&1; then
  ok "telegram-webhook.service is running"
else
  warn "telegram-webhook.service not running — start: systemctl --user start telegram-webhook.service"
fi

if curl -sf http://localhost:8765/health >/dev/null 2>&1; then
  ok "webhook handler responding on port 8765"
else
  warn "webhook handler not responding on port 8765"
fi

# ─── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════"
echo "  SUMMARY"
echo "  OK:   $PASS"
echo "  WARN: $WARN"
echo "  FAIL: $FAIL"
echo "═══════════════════════════════════════"

if [ "$FAIL" -gt 0 ]; then
  echo "  STATUS: FAIL — fix the FAIL items before running sessions"
  exit 1
elif [ "$WARN" -gt 0 ]; then
  echo "  STATUS: WARN — review warnings, may be normal for fresh install"
  exit 0
else
  echo "  STATUS: OK — all checks passed"
  exit 0
fi
