#!/usr/bin/env bash
# emergency_mode.sh
# Toggle emergency/daytime mode for the node.
#
# Usage:
#   bash tools/emergency_mode.sh on [interval_min] [reason]
#   bash tools/emergency_mode.sh off
#
# on  — Activates emergency mode:
#         - Bypasses 23:00-06:00 time window restriction
#         - Bypasses session count limits (effectively unlimited sessions)
#         - Installs a user-level systemd timer at the specified interval
#         - First session starts in ~1 minute
#       interval_min: session interval in minutes (default: 60)
#       reason:       free-text label written to the flag file (default: "daytime showcase mode")
#
# off — Deactivates emergency mode:
#         - Removes the emergency flag (restores time window enforcement)
#         - Stops and removes the emergency timer
#
# Examples:
#   bash tools/emergency_mode.sh on            # 60-min interval
#   bash tools/emergency_mode.sh on 30         # 30-min interval
#   bash tools/emergency_mode.sh on 15 "urgent"
#   bash tools/emergency_mode.sh off

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STATE_DIR="$PROJECT_DIR/state"
SCRIPTS_DIR="$PROJECT_DIR/scripts"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
EMERGENCY_FLAG="$STATE_DIR/emergency_mode.active"
INSTANCE="$(basename "$PROJECT_DIR")"
TIMER_UNIT="emergency-agent@${INSTANCE}.timer"

if [ $# -lt 1 ]; then
  echo "Usage: emergency_mode.sh <on|off> [interval_min] [reason]" >&2
  exit 1
fi

ACTION="$1"

# --- on ---
if [ "$ACTION" = "on" ]; then
  INTERVAL_MIN="${2:-60}"
  REASON="${3:-daytime showcase mode}"
  TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S %Z')

  # Validate interval is a positive integer.
  if ! [[ "$INTERVAL_MIN" =~ ^[0-9]+$ ]] || [ "$INTERVAL_MIN" -lt 1 ]; then
    echo "ERROR: interval must be a positive integer (minutes). Got: $INTERVAL_MIN" >&2
    exit 1
  fi

  echo "=== Enabling Emergency Mode ==="
  echo "Interval: ${INTERVAL_MIN}min between sessions"
  echo "Reason:   $REASON"
  echo "Time:     $TIMESTAMP"
  echo ""

  # 1. Write the emergency flag.
  echo "$REASON (activated: $TIMESTAMP)" > "$EMERGENCY_FLAG"
  echo "[OK] Emergency flag written: $EMERGENCY_FLAG"

  # 2. Install user-level systemd units.
  # emergency-agent@.service is an instance template (%i = this clone's
  # directory name, ${INSTANCE}) -- install the template once; the timer
  # interval is written dynamically per-instance so no manual edits to the
  # .timer file are needed, just pass a different interval to this script.
  mkdir -p "$SYSTEMD_USER_DIR"
  cp "$SCRIPTS_DIR/executional/emergency-agent@.service" "$SYSTEMD_USER_DIR/emergency-agent@.service"
  cat > "$SYSTEMD_USER_DIR/${TIMER_UNIT}" <<EOF
[Unit]
Description=Emergency Agent Timer — ${INSTANCE} — fires every ${INTERVAL_MIN} minutes

[Timer]
OnActiveSec=1min
OnUnitActiveSec=${INTERVAL_MIN}min
Persistent=false

[Install]
WantedBy=timers.target
EOF
  echo "[OK] Systemd units installed to $SYSTEMD_USER_DIR (instance: ${INSTANCE}, interval: ${INTERVAL_MIN}min)"

  # 3. Enable linger so user services survive without an active login session.
  loginctl enable-linger "$(whoami)" 2>/dev/null && echo "[OK] Linger enabled for $(whoami)" \
    || echo "[WARN] Could not enable linger (may already be enabled or requires sudo)"

  # 4. Reload and start the timer.
  _uid="$(id -u)"
  export XDG_RUNTIME_DIR="/run/user/${_uid}"
  export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${_uid}/bus"

  systemctl --user daemon-reload
  echo "[OK] systemd user daemon reloaded"

  systemctl --user enable "$TIMER_UNIT"
  systemctl --user start "$TIMER_UNIT"
  echo "[OK] $TIMER_UNIT enabled and started"

  echo ""
  echo "=== Emergency Mode ACTIVE ==="
  echo "First session fires in ~1 minute."
  echo "Subsequent sessions fire every ${INTERVAL_MIN} minutes."
  echo "Sessions log to: $PROJECT_DIR/logs/"
  echo ""
  echo "To check timer status:"
  echo "  systemctl --user status $TIMER_UNIT"
  echo ""
  echo "To disable emergency mode:"
  echo "  bash $PROJECT_DIR/tools/executional/emergency_mode.sh off"

# --- off ---
elif [ "$ACTION" = "off" ]; then
  echo "=== Disabling Emergency Mode ==="

  # 1. Stop and disable the timer.
  _uid="$(id -u)"
  export XDG_RUNTIME_DIR="/run/user/${_uid}"
  export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${_uid}/bus"

  if systemctl --user is-active "$TIMER_UNIT" &>/dev/null; then
    systemctl --user stop "$TIMER_UNIT"
    echo "[OK] $TIMER_UNIT stopped"
  else
    echo "[--] $TIMER_UNIT was not running"
  fi

  if systemctl --user is-enabled "$TIMER_UNIT" &>/dev/null; then
    systemctl --user disable "$TIMER_UNIT"
    echo "[OK] $TIMER_UNIT disabled"
  fi

  # 2. Remove the installed unit files.
  rm -f "$SYSTEMD_USER_DIR/${TIMER_UNIT}" "$SYSTEMD_USER_DIR/emergency-agent@.service"
  echo "[OK] Systemd unit files removed"

  systemctl --user daemon-reload
  echo "[OK] systemd user daemon reloaded"

  # 3. Remove the emergency flag.
  if [ -f "$EMERGENCY_FLAG" ]; then
    reason=$(cat "$EMERGENCY_FLAG")
    rm -f "$EMERGENCY_FLAG"
    echo "[OK] Emergency flag removed (was: $reason)"
  else
    echo "[--] Emergency flag was not present"
  fi

  # 4. Reset the emergency session counter.
  echo "0" > "$STATE_DIR/sessions_emergency.count"
  echo "[OK] sessions_emergency.count reset to 0"

  echo ""
  echo "=== Emergency Mode DISABLED ==="
  echo "Normal 23:00-06:00 scheduling resumes."
  echo "Night agent will next fire at scheduled timer times."

else
  echo "ERROR: unknown action '$ACTION'. Use 'on' or 'off'." >&2
  echo "Usage: emergency_mode.sh <on|off> [interval_min] [reason]" >&2
  exit 1
fi
