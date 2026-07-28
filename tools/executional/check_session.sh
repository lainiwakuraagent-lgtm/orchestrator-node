#!/usr/bin/env bash
# check_session.sh — Unified session health checks.
#
# Subcommands:
#   --usage    Check real account-level subscription utilization (rate-limit headers)
#   --context  Estimate current session's context window fill percentage
#   --time     Report wall-clock time and nightly work-window status
#
# Each subcommand exits 0 in all cases (including internal errors) and prints
# an ACTION line for the caller to parse. This preserves the exact output
# format of the three scripts this replaces (check_usage.sh, check_context.sh,
# check_time.sh), so existing callers only need their invocation updated.
#
# Usage:
#   bash tools/check_session.sh --usage
#   bash tools/check_session.sh --context
#   bash tools/check_session.sh --time

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# =============================================================================
# --usage — real account-level subscription utilization
#
# Makes a minimal API call (1 token, cheapest model) and reads the rate-limit
# headers that Anthropic returns on every response.
#
# Thresholds (configurable via env vars):
#   USAGE_THRESHOLD_5H  -- rolling 5-hour utilization (0.0-1.0). Default: 0.70
#   USAGE_THRESHOLD_7D  -- rolling 7-day utilization  (0.0-1.0). Default: 0.80
# =============================================================================
cmd_usage() {
  local THRESHOLD_5H="${USAGE_THRESHOLD_5H:-0.70}"
  local THRESHOLD_7D="${USAGE_THRESHOLD_7D:-0.80}"
  local PROBE_MODEL="claude-haiku-4-5-20251001"
  local CREDENTIALS_FILE="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/.credentials.json"
  local API_URL="https://api.anthropic.com/v1/messages"

  if [ ! -f "$CREDENTIALS_FILE" ]; then
    echo "usage_check_status: error"
    echo "reason: credentials file not found at $CREDENTIALS_FILE"
    echo "ACTION: cannot check usage -- treat as unknown, proceed with caution."
    return 0
  fi

  local access_token
  access_token=$(python3 - "$CREDENTIALS_FILE" <<'EOF'
import sys, json
try:
    with open(sys.argv[1]) as f:
        d = json.load(f)
    print(d["claudeAiOauth"]["accessToken"])
except Exception as e:
    print("ERROR: " + str(e), file=sys.stderr)
    sys.exit(1)
EOF
)

  local header_file body_file
  header_file=$(mktemp)
  body_file=$(mktemp)
  trap 'rm -f "$header_file" "$body_file"' RETURN

  local http_code
  http_code=$(curl -s -o "$body_file" -D "$header_file" -w "%{http_code}" \
      --max-time 15 \
      -X POST "$API_URL" \
      -H "Authorization: Bearer $access_token" \
      -H "Content-Type: application/json" \
      -H "anthropic-version: 2023-06-01" \
      -d "{\"model\":\"$PROBE_MODEL\",\"max_tokens\":1,\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}" \
      2>/dev/null || echo "000")

  if [ "$http_code" = "000" ]; then
    echo "usage_check_status: error"
    echo "reason: curl failed (network unreachable or timeout)"
    echo "ACTION: cannot check usage -- treat as unknown, proceed with caution."
    return 0
  fi

  if [ "$http_code" = "401" ] || [ "$http_code" = "403" ]; then
    echo "usage_check_status: error"
    echo "reason: auth error (HTTP $http_code) -- token may be expired"
    echo "ACTION: cannot check usage -- treat as unknown, proceed with caution."
    return 0
  fi

  local util_5h util_7d status_5h status_7d reset_5h reset_7d
  util_5h=$(grep -i '^anthropic-ratelimit-unified-5h-utilization:' "$header_file" \
      | awk '{print $2}' | tr -d '[:space:]' || echo "")
  util_7d=$(grep -i '^anthropic-ratelimit-unified-7d-utilization:' "$header_file" \
      | awk '{print $2}' | tr -d '[:space:]' || echo "")
  status_5h=$(grep -i '^anthropic-ratelimit-unified-5h-status:' "$header_file" \
      | awk '{print $2}' | tr -d '[:space:]' || echo "unknown")
  status_7d=$(grep -i '^anthropic-ratelimit-unified-7d-status:' "$header_file" \
      | awk '{print $2}' | tr -d '[:space:]' || echo "unknown")
  reset_5h=$(grep -i '^anthropic-ratelimit-unified-5h-reset:' "$header_file" \
      | awk '{print $2}' | tr -d '[:space:]' || echo "")
  reset_7d=$(grep -i '^anthropic-ratelimit-unified-7d-reset:' "$header_file" \
      | awk '{print $2}' | tr -d '[:space:]' || echo "")

  if [ -z "$util_5h" ] && [ -z "$util_7d" ]; then
    echo "usage_check_status: error"
    echo "reason: no utilization headers in response (HTTP $http_code) -- API may have changed"
    echo "ACTION: cannot check usage -- treat as unknown, proceed with caution."
    return 0
  fi

  local reset_5h_human="" reset_7d_human=""
  if [ -n "$reset_5h" ] && [ "$reset_5h" -gt 0 ] 2>/dev/null; then
    reset_5h_human=$(date -d "@$reset_5h" '+%Y-%m-%d %H:%M %Z' 2>/dev/null || echo "$reset_5h")
  fi
  if [ -n "$reset_7d" ] && [ "$reset_7d" -gt 0 ] 2>/dev/null; then
    reset_7d_human=$(date -d "@$reset_7d" '+%Y-%m-%d %H:%M %Z' 2>/dev/null || echo "$reset_7d")
  fi

  echo "usage_check_status: ok"
  echo "utilization_5h: ${util_5h:-unknown}  (status: $status_5h, resets: ${reset_5h_human:-unknown})"
  echo "utilization_7d: ${util_7d:-unknown}  (status: $status_7d, resets: ${reset_7d_human:-unknown})"
  echo "threshold_5h: $THRESHOLD_5H"
  echo "threshold_7d: $THRESHOLD_7D"

  local block_reason=""

  if [ -n "$util_5h" ]; then
    local over_5h
    over_5h=$(python3 -c "print('yes' if float('$util_5h') > float('$THRESHOLD_5H') else 'no')" 2>/dev/null || echo "no")
    if [ "$over_5h" = "yes" ]; then
      local pct_5h thr_5h
      pct_5h=$(python3 -c "print(f'{float(\"$util_5h\")*100:.0f}')")
      thr_5h=$(python3 -c "print(f'{float(\"$THRESHOLD_5H\")*100:.0f}')")
      block_reason="5h utilization ${pct_5h}% exceeds threshold ${thr_5h}%"
    fi
  fi

  if [ -n "$util_7d" ] && [ -z "$block_reason" ]; then
    local over_7d
    over_7d=$(python3 -c "print('yes' if float('$util_7d') > float('$THRESHOLD_7D') else 'no')" 2>/dev/null || echo "no")
    if [ "$over_7d" = "yes" ]; then
      local pct_7d thr_7d
      pct_7d=$(python3 -c "print(f'{float(\"$util_7d\")*100:.0f}')")
      thr_7d=$(python3 -c "print(f'{float(\"$THRESHOLD_7D\")*100:.0f}')")
      block_reason="7d utilization ${pct_7d}% exceeds threshold ${thr_7d}%"
    fi
  fi

  if [ -n "$block_reason" ]; then
    echo "ACTION: usage limit exceeded ($block_reason) -- do not launch session."
  else
    echo "ACTION: usage within limits, ok to proceed."
  fi
}

# =============================================================================
# --context — estimated context window fill percentage
#
# METHOD (v2 — character-level parsing): parses the current session's .jsonl
# transcript and sums the character count of all message content fields.
# Content chars / 4 ~= token count. This is ~6-7x more accurate than the
# older file_bytes/4 estimate, which is retained here only for reference.
#
# KNOWN LIMITATION: chars/4 overestimates tokens for JSON/code-heavy content
# (closer to 2-3 chars/token). Treat any reading < 50% as "comfortable" and
# > 70% as "prepare to wrap up."
# =============================================================================
cmd_context() {
  local CONTEXT_WINDOW_TOKENS=200000
  local CHARS_PER_TOKEN_ESTIMATE=4
  local WARN_PCT=70

  local CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
  local PROJECTS_DIR="$CLAUDE_DIR/projects"

  if [ ! -d "$PROJECTS_DIR" ]; then
    echo "context_pct_estimate: unknown"
    echo "reason: no Claude Code projects directory found at $PROJECTS_DIR"
    echo "ACTION: cannot estimate context -- treat as unknown, use time limits as primary guard."
    return 0
  fi

  local latest_transcript
  latest_transcript=$(find "$PROJECTS_DIR" -name '*.jsonl' -type f \
    -printf '%T@ %p\n' 2>/dev/null \
    | sort -rn | head -n1 | cut -d' ' -f2-)

  if [ -z "${latest_transcript:-}" ]; then
    echo "context_pct_estimate: unknown"
    echo "reason: no transcript file found"
    echo "ACTION: cannot estimate context -- treat as unknown, use time limits as primary guard."
    return 0
  fi

  local file_bytes
  file_bytes=$(stat -c%s "$latest_transcript" 2>/dev/null || stat -f%z "$latest_transcript")

  local content_chars
  content_chars=$(python3 - "$latest_transcript" <<'PYEOF'
import sys, json

total = 0
with open(sys.argv[1], 'r', errors='replace') as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = obj.get('message', obj)
        content = msg.get('content', '')
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    text = block.get('text', '') or block.get('input', '')
                    if isinstance(text, str):
                        total += len(text)
                    elif isinstance(text, dict):
                        total += len(json.dumps(text))
print(total)
PYEOF
)

  local estimated_tokens=$(( content_chars / CHARS_PER_TOKEN_ESTIMATE ))
  local pct=$(( estimated_tokens * 100 / CONTEXT_WINDOW_TOKENS ))

  local file_est_tokens=$(( file_bytes / CHARS_PER_TOKEN_ESTIMATE ))
  local file_pct=$(( file_est_tokens * 100 / CONTEXT_WINDOW_TOKENS ))

  echo "transcript_file: $latest_transcript"
  echo "transcript_bytes: $file_bytes"
  echo "file_size_est_pct: ${file_pct}%  (old method -- unreliable, ~6-7x inflated)"
  echo "content_chars: $content_chars"
  echo "estimated_tokens: $estimated_tokens"
  echo "context_pct_estimate: ${pct}%  (content-char method)"

  if [ "$pct" -ge "$WARN_PCT" ]; then
    echo "ACTION: context usage estimated above ${WARN_PCT}% -- stop new work, begin shutdown and memory write now."
  else
    echo "ACTION: context within limits, ok to continue."
  fi
}

# =============================================================================
# --time — wall-clock time and nightly work-window status
#
# Never trust an LLM's internal sense of time -- this is the single source
# of truth. Emergency mode override: if state/emergency_mode.active exists,
# the work window is considered always open.
# =============================================================================
cmd_time() {
  local EMERGENCY_FLAG="$PROJECT_DIR/state/emergency_mode.active"

  local WINDOW_START_HOUR=23   # 23:00
  local WINDOW_END_HOUR=6      # 06:00

  local now_epoch now_human hour minute
  now_epoch=$(date +%s)
  now_human=$(date '+%Y-%m-%d %H:%M:%S %Z')
  hour=$(date +%H)
  hour=$((10#$hour))  # force base-10 (avoid octal parsing of "08", "09")
  minute=$(date +%M)
  minute=$((10#$minute))

  if [ -f "$EMERGENCY_FLAG" ]; then
    local emergency_reason
    emergency_reason=$(cat "$EMERGENCY_FLAG" 2>/dev/null | head -1 || echo "active")
    echo "current_time: $now_human"
    echo "in_work_window: true"
    echo "minutes_remaining_until_window_close: 9999"
    echo "emergency_mode: ACTIVE ($emergency_reason)"
    echo "ACTION: emergency mode -- window always open, no time-based shutdown."
    return 0
  fi

  local in_window
  if [ "$hour" -ge "$WINDOW_START_HOUR" ] || [ "$hour" -lt "$WINDOW_END_HOUR" ]; then
    in_window="true"
  else
    in_window="false"
  fi

  local end_epoch
  if [ "$hour" -lt "$WINDOW_END_HOUR" ]; then
    end_epoch=$(date -d "today ${WINDOW_END_HOUR}:00:00" +%s)
  else
    end_epoch=$(date -d "tomorrow ${WINDOW_END_HOUR}:00:00" +%s)
  fi
  local minutes_remaining=$(( (end_epoch - now_epoch) / 60 ))

  echo "current_time: $now_human"
  echo "in_work_window: $in_window"
  echo "minutes_remaining_until_window_close: $minutes_remaining"

  if [ "$in_window" = "false" ]; then
    echo "ACTION: outside work window -- do not proceed with goal work, shut down now."
  elif [ "$minutes_remaining" -lt 15 ]; then
    echo "ACTION: less than 15 minutes remain -- treat as window closing, begin shutdown."
  fi
}

case "${1:-}" in
  --usage) cmd_usage ;;
  --context) cmd_context ;;
  --time) cmd_time ;;
  *)
    echo "Usage: check_session.sh --usage|--context|--time" >&2
    exit 1
    ;;
esac
