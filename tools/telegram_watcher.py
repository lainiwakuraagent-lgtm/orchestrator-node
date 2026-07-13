#!/usr/bin/env python3
"""
telegram_watcher.py — Blocking Telegram message watcher for conversational sessions.

Polls Telegram getUpdates API with long-polling. Blocks until a new message
arrives from the allowed user, then prints the message as JSON to stdout and exits.

IMPORTANT: getUpdates and webhooks cannot coexist (Telegram returns 409 Conflict).
conversation.sh MUST call deleteWebhook before launching this script, and restore
the webhook after the conversation session ends.

Usage:
    python3 tools/telegram_watcher.py

State files:
    state/conversation/last_update_id.txt — persists update_id across calls

Token source: PROJECT_DIR/identity/agent.env if present, else ~/.claude/.env
Exit codes:
    0 — new message received, JSON printed to stdout
    1 — interrupted or timeout (caller should retry)
    2 — fatal error (token missing, etc.)
"""

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
AGENT_ENV = PROJECT_DIR / "identity" / "agent.env"
ENV_FILE = AGENT_ENV if AGENT_ENV.exists() else Path.home() / ".claude" / ".env"
LAST_UPDATE_FILE = PROJECT_DIR / "state" / "conversation" / "last_update_id.txt"
WATCHER_PID_FILE = PROJECT_DIR / "state" / "conversation" / "watcher.pid"
OUTBOX_FILE = PROJECT_DIR / "state" / "conversation" / "outbox.json"

# Long-poll timeout (seconds). Telegram holds the connection open for this long
# if there are no updates. Shorter = more reconnects; longer = more blocking.
LONG_POLL_TIMEOUT = 25

# Retry delay on network error
RETRY_DELAY = 3


def load_env() -> tuple[str, str]:
    """Load token and allowed chat ID from env file.

    Checks PROJECT_DIR/identity/agent.env first (agent-specific credentials),
    then falls back to ~/.claude/.env (lain default). Supports both
    TELEGRAM_CHAT_ID (agent.env) and TELEGRAM_ALLOWED_USERS (~/.claude/.env).
    """
    token = ""
    chat_id = ""
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                token = line.split("=", 1)[1].strip()
            elif line.startswith("TELEGRAM_CHAT_ID="):
                chat_id = line.split("=", 1)[1].strip()
            elif line.startswith("TELEGRAM_ALLOWED_USERS=") and not chat_id:
                chat_id = line.split("=", 1)[1].strip()
    return token, chat_id


def load_last_update_id() -> int:
    LAST_UPDATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if LAST_UPDATE_FILE.exists():
        try:
            return int(LAST_UPDATE_FILE.read_text().strip())
        except ValueError:
            pass
    return 0


def save_last_update_id(update_id: int) -> None:
    LAST_UPDATE_FILE.write_text(str(update_id))


def get_updates(token: str, offset: int) -> list:
    """Call getUpdates with long-polling. Returns list of update dicts."""
    url = (
        f"https://api.telegram.org/bot{token}/getUpdates"
        f"?offset={offset}&limit=10&timeout={LONG_POLL_TIMEOUT}"
    )
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=LONG_POLL_TIMEOUT + 5) as resp:
        data = json.loads(resp.read())
    if not data.get("ok"):
        raise RuntimeError(f"getUpdates error: {data}")
    return data.get("result", [])


def write_pid() -> None:
    WATCHER_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Kill previous instance if running — prevents 409 Conflict on getUpdates
    if WATCHER_PID_FILE.exists():
        try:
            old_pid = int(WATCHER_PID_FILE.read_text().strip())
            os.kill(old_pid, signal.SIGTERM)
            time.sleep(0.5)  # let it die before we take over
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    WATCHER_PID_FILE.write_text(str(os.getpid()))


def remove_pid() -> None:
    try:
        WATCHER_PID_FILE.unlink()
    except FileNotFoundError:
        pass


def forward_outbox() -> None:
    """Check outbox.json for pending messages from the execution layer and forward via Telegram.

    Reads OUTBOX_FILE, sends each unsent entry via telegram_send.sh, marks them sent.
    Non-fatal — errors are logged to stderr but do not interrupt the watcher loop.
    """
    if not OUTBOX_FILE.exists():
        return
    try:
        entries = json.loads(OUTBOX_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return

    changed = False
    for entry in entries:
        if entry.get("sent"):
            continue
        content = entry.get("content", "").strip()
        if not content:
            entry["sent"] = True
            changed = True
            continue
        try:
            send_proc = subprocess.run(
                ["bash", str(SCRIPT_DIR / "telegram_send.sh")],
                input=content, text=True, capture_output=True, timeout=35,
                env={**os.environ, "SKIP_TTS": "1"},
            )
            if send_proc.returncode != 0:
                print(f"outbox forward failed: {send_proc.stderr[:100]}", file=sys.stderr)
                continue  # leave unsent, retry next cycle
        except Exception as e:
            print(f"outbox forward error: {e}", file=sys.stderr)
            continue
        entry["sent"] = True
        changed = True

    if changed:
        try:
            OUTBOX_FILE.write_text(json.dumps(entries, indent=2))
        except OSError as e:
            print(f"outbox write error: {e}", file=sys.stderr)


def dispatch_command(text: str) -> None:
    """Handle a /command: dispatch via command_dispatcher.py, send response via telegram_send.sh.

    Does NOT emit anything to stdout and does NOT exit — caller continues polling.
    """
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "command_dispatcher.py"), text],
            capture_output=True, text=True, timeout=30,
        )
        response = result.stdout.strip()
        if not response:
            response = f"@Lain — command error: {result.stderr[:200]}" if result.stderr else "@Lain — no response"
    except Exception as e:
        response = f"@Lain — dispatch failed: {e}"

    try:
        env = {**os.environ, "SKIP_TTS": "1"}  # commands never get TTS
        send_proc = subprocess.run(
            ["bash", str(SCRIPT_DIR / "telegram_send.sh")],
            input=response, text=True, capture_output=True, timeout=35, env=env,
        )
        if send_proc.returncode != 0:
            print(f"telegram_send failed: {send_proc.stderr[:100]}", file=sys.stderr)
    except Exception as e:
        print(f"telegram_send error: {e}", file=sys.stderr)


def main() -> int:
    token, allowed_chat = load_env()
    if not token:
        print("ERROR: TELEGRAM_BOT_TOKEN not found", file=sys.stderr)
        return 2

    write_pid()

    def _cleanup(signum, frame):  # noqa: ANN001
        remove_pid()
        sys.exit(1)

    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT, _cleanup)

    offset = load_last_update_id()

    while True:
        try:
            updates = get_updates(token, offset)
        except KeyboardInterrupt:
            return 1
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            print(f"Network error: {e} — retrying in {RETRY_DELAY}s", file=sys.stderr)
            time.sleep(RETRY_DELAY)
            continue
        except Exception as e:
            print(f"Unexpected error: {e} — retrying in {RETRY_DELAY}s", file=sys.stderr)
            time.sleep(RETRY_DELAY)
            continue

        for upd in updates:
            update_id = upd["update_id"]
            # Advance offset past this update regardless of whether we process it
            offset = update_id + 1
            save_last_update_id(offset)

            msg = upd.get("message") or upd.get("channel_post")
            if not msg:
                continue

            text = msg.get("text", "")
            if not text:
                continue

            # Filter to allowed chat only
            chat_id = str(msg.get("chat", {}).get("id", ""))
            if allowed_chat and chat_id != allowed_chat:
                continue

            # Slash commands go to dispatcher, never to the agent
            if text.startswith("/"):
                dispatch_command(text)
                continue  # keep polling — don't emit to agent, don't exit

            # Found a message for us — print and exit
            out = {
                "update_id": update_id,
                "message_id": msg.get("message_id"),
                "chat_id": chat_id,
                "from": msg.get("from", {}).get("username", "unknown"),
                "text": text,
                "date": msg.get("date", 0),
            }
            print(json.dumps(out))
            remove_pid()
            return 0

        # Check outbox for pending execution-layer messages to forward
        forward_outbox()

        # No relevant updates in this batch — loop continues (long-poll)


if __name__ == "__main__":
    sys.exit(main())
