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

Token source: TELEGRAM_BOT_TOKEN_FILE env var → TELEGRAM_BOT_TOKEN env var →
    identity/agent.env → ~/.claude/.env (in that priority order)
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
_FALLBACK_ENV_FILE = Path.home() / ".claude" / ".env"
LAST_UPDATE_FILE = PROJECT_DIR / "state" / "conversation" / "last_update_id.txt"
WATCHER_PID_FILE = PROJECT_DIR / "state" / "conversation" / "watcher.pid"
OUTBOX_FILE = PROJECT_DIR / "state" / "conversation" / "outbox.json"
NEXUS_ROUTING_FILE = PROJECT_DIR / "state" / "nexus_routing.json"

# Long-poll timeout (seconds). Telegram holds the connection open for this long
# if there are no updates. Shorter = more reconnects; longer = more blocking.
LONG_POLL_TIMEOUT = 25

# Retry delay on network error
RETRY_DELAY = 3


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a key=value env file, skipping comments."""
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip()
    return result


def load_env() -> tuple[str, str]:
    """Load token and allowed chat ID.

    Priority order for token:
      1. TELEGRAM_BOT_TOKEN_FILE env var → read token from that file
      2. TELEGRAM_BOT_TOKEN env var (direct)
      3. PROJECT_DIR/identity/agent.env  → TELEGRAM_BOT_TOKEN
      4. ~/.claude/.env                  → TELEGRAM_BOT_TOKEN

    Priority order for chat_id:
      1. TELEGRAM_CHAT_ID env var (direct)
      2. PROJECT_DIR/identity/agent.env  → TELEGRAM_CHAT_ID or TELEGRAM_ALLOWED_USERS
      3. ~/.claude/.env                  → TELEGRAM_ALLOWED_USERS
    """
    token = ""
    chat_id = ""

    # 1. Token file override (agent_config.env sets TELEGRAM_BOT_TOKEN_FILE)
    token_file = os.environ.get("TELEGRAM_BOT_TOKEN_FILE", "")
    if token_file:
        try:
            token = Path(token_file).read_text().strip()
        except OSError:
            pass

    # 2. Direct env vars
    if not token:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    # 3. identity/agent.env (agent-specific credentials, present in orchestrator etc.)
    if not token or not chat_id:
        agent_env = _parse_env_file(PROJECT_DIR / "identity" / "agent.env")
        if not token:
            token = agent_env.get("TELEGRAM_BOT_TOKEN", "")
        if not chat_id:
            chat_id = agent_env.get("TELEGRAM_CHAT_ID", "") or agent_env.get("TELEGRAM_ALLOWED_USERS", "")

    # 4. Fallback: ~/.claude/.env (Lain's default credentials)
    if not token or not chat_id:
        fallback = _parse_env_file(_FALLBACK_ENV_FILE)
        if not token:
            token = fallback.get("TELEGRAM_BOT_TOKEN", "")
        if not chat_id:
            chat_id = fallback.get("TELEGRAM_ALLOWED_USERS", "")

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


def _load_nexus_routing() -> dict:
    """Load agent-name → nexus-conversation-id mapping from state/nexus_routing.json."""
    if not NEXUS_ROUTING_FILE.exists():
        return {}
    try:
        return json.loads(NEXUS_ROUTING_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _send_nexus(agent_name: str, content: str) -> bool:
    """Send content to a named agent via Nexus DM. Returns True on success."""
    routing = _load_nexus_routing()
    convo_id = routing.get(agent_name)
    if not convo_id:
        print(f"outbox nexus: no routing entry for agent '{agent_name}'", file=sys.stderr)
        return False
    try:
        send_proc = subprocess.run(
            ["bash", str(SCRIPT_DIR / "nexus_send.sh"), convo_id],
            input=content, text=True, capture_output=True, timeout=35,
        )
        if send_proc.returncode != 0:
            print(f"outbox nexus send failed: {send_proc.stderr[:100]}", file=sys.stderr)
            return False
    except Exception as e:
        print(f"outbox nexus send error: {e}", file=sys.stderr)
        return False
    return True


def forward_outbox() -> None:
    """Check outbox.json for pending entries and route them by type+to.

    Routing:
        type=message  + to=owner        → Telegram
        type=question + to=owner        → Telegram (prefixed "Question for you:")
        type=message  + to=agent:<name> → Nexus DM
        type=question + to=agent:<name> → Nexus DM (question framing)
        (no type/to fields)             → Telegram (backwards compatible)

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

        msg_type = entry.get("type", "message")
        to = entry.get("to", "owner")
        ok = False

        if to == "owner" or not to.startswith("agent:"):
            # Route to Telegram
            if msg_type == "question":
                content = f"Question for you:\n{content}"
            try:
                send_proc = subprocess.run(
                    ["bash", str(SCRIPT_DIR / "telegram_send.sh")],
                    input=content, text=True, capture_output=True, timeout=35,
                    env={**os.environ, "SKIP_TTS": "1"},
                )
                if send_proc.returncode != 0:
                    print(f"outbox telegram failed: {send_proc.stderr[:100]}", file=sys.stderr)
                else:
                    ok = True
            except Exception as e:
                print(f"outbox telegram error: {e}", file=sys.stderr)
        else:
            # Route to agent via Nexus DM
            agent_name = to[len("agent:"):]
            if msg_type == "question":
                content = f"[question] {content}"
            ok = _send_nexus(agent_name, content)

        if ok:
            entry["sent"] = True
            changed = True
        # else: leave unsent, retry next cycle

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
