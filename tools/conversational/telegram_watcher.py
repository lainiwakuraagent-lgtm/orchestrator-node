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
PROJECT_DIR = SCRIPT_DIR.parent.parent
_FALLBACK_ENV_FILE = Path.home() / ".claude" / ".env"
LAST_UPDATE_FILE = PROJECT_DIR / "state" / "conversation" / "last_update_id.txt"
WATCHER_PID_FILE = PROJECT_DIR / "state" / "conversation" / "watcher.pid"
OUTBOX_FILE = PROJECT_DIR / "state" / "conversation" / "outbox.json"

# Long-poll timeout (seconds). Telegram holds the connection open for this long
# if there are no updates. Shorter = more reconnects; longer = more blocking.
LONG_POLL_TIMEOUT = 25

# Retry delay on network error
RETRY_DELAY = 3

INBOX_FILES_DIR = PROJECT_DIR / "inbox" / "files"
INBOX_FILE = PROJECT_DIR / "inbox" / "pending.json"


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
            old_content = WATCHER_PID_FILE.read_text().strip()
            # Support both "pid" (old) and "pid:wrapper_pid" (new) formats
            if ':' in old_content:
                old_watcher_str, old_wrapper_str = old_content.split(':', 1)
                old_pid = int(old_watcher_str)
                file_wrapper_pid = int(old_wrapper_str) if old_wrapper_str.isdigit() else 0
            else:
                old_pid = int(old_content)
                file_wrapper_pid = 0

            # Kill stored wrapper first — works even when watcher already exited and
            # removed the PID file on its own (the main orphan-recurrence root cause).
            if file_wrapper_pid > 1:
                try:
                    os.kill(file_wrapper_pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass

            # Also discover wrapper via ps if watcher is still alive
            try:
                ppid_out = subprocess.check_output(
                    ["ps", "-o", "ppid=", "-p", str(old_pid)], stderr=subprocess.DEVNULL
                )
                live_wrapper_pid = int(ppid_out.strip())
                if live_wrapper_pid > 1 and live_wrapper_pid != file_wrapper_pid:
                    os.kill(live_wrapper_pid, signal.SIGTERM)
            except Exception:
                pass

            # Kill the watcher itself
            try:
                os.kill(old_pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            time.sleep(0.5)  # let it die before we take over
        except (ValueError, ProcessLookupError, PermissionError):
            pass

    # Write new PID with parent (bash wrapper) PID so kill_stale_watcher can find it
    # even if this process exits cleanly before the bash wrapper does.
    my_pid = os.getpid()
    try:
        ppid_out = subprocess.check_output(
            ["ps", "-o", "ppid=", "-p", str(my_pid)], stderr=subprocess.DEVNULL
        )
        my_wrapper_pid = int(ppid_out.strip())
    except Exception:
        my_wrapper_pid = 0
    WATCHER_PID_FILE.write_text(f"{my_pid}:{my_wrapper_pid}")


def remove_pid() -> None:
    try:
        WATCHER_PID_FILE.unlink()
    except FileNotFoundError:
        pass


def forward_outbox() -> None:
    """Check outbox.json for pending entries and route them by type+to.

    Routing:
        type=message  + to=owner  → Telegram
        type=question + to=owner  → Telegram (prefixed "Question for you:")
        (no type/to fields)       → Telegram (backwards compatible)

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

        if ok:
            entry["sent"] = True
            changed = True
        # else: leave unsent, retry next cycle

    if changed:
        try:
            OUTBOX_FILE.write_text(json.dumps(entries, indent=2))
        except OSError as e:
            print(f"outbox write error: {e}", file=sys.stderr)


def _get_file_path(token: str, file_id: str) -> tuple[str, str]:
    """Call getFile API — returns (tg_file_path, filename)."""
    url = f"https://api.telegram.org/bot{token}/getFile?file_id={file_id}"
    with urllib.request.urlopen(urllib.request.Request(url), timeout=30) as resp:
        data = json.loads(resp.read())
    if not data.get("ok"):
        raise RuntimeError(f"getFile error: {data}")
    tg_path = data["result"]["file_path"]
    filename = tg_path.split("/")[-1]
    return tg_path, filename


def _download_file(token: str, tg_path: str) -> bytes:
    """Download file bytes from Telegram CDN."""
    url = f"https://api.telegram.org/file/bot{token}/{tg_path}"
    with urllib.request.urlopen(urllib.request.Request(url), timeout=60) as resp:
        return resp.read()


def _save_inbox_file(content: bytes, filename: str, caption: str, file_type: str, mime_type: str, ts: int, from_: str) -> str:
    """Save file to inbox/files/, append manifest entry to pending.json. Returns relative path."""
    INBOX_FILES_DIR.mkdir(parents=True, exist_ok=True)
    dest = INBOX_FILES_DIR / f"{ts}_{filename}"
    dest.write_bytes(content)
    rel_path = str(dest.relative_to(PROJECT_DIR))

    entry = {
        "source": "telegram",
        "from": from_,
        "content": caption if caption else f"[file: {filename}, no caption]",
        "timestamp": ts,
        "type": "file_delivery",
        "file_path": rel_path,
        "file_name": filename,
        "file_type": file_type,
        "mime_type": mime_type,
        "processed": False,
    }

    entries: list = []
    if INBOX_FILE.exists():
        try:
            entries = json.loads(INBOX_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            entries = []
    entries.append(entry)
    tmp = INBOX_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, indent=2))
    tmp.rename(INBOX_FILE)
    return rel_path


def handle_file_message(token: str, msg: dict, allowed_chat: str) -> None:
    """Handle a Telegram photo or document message — download and queue in inbox."""
    chat_id = str(msg.get("chat", {}).get("id", ""))
    if allowed_chat and chat_id != allowed_chat:
        return

    ts = msg.get("date", int(time.time()))
    from_ = msg.get("from", {}).get("username", "unknown")
    caption = msg.get("caption", "")

    photo = msg.get("photo")
    document = msg.get("document")

    if photo:
        # photo is an array sorted by size; last entry = largest
        largest = photo[-1]
        file_id = largest["file_id"]
        file_type = "photo"
        mime_type = "image/jpeg"
        tg_path, filename = _get_file_path(token, file_id)
    elif document:
        file_id = document["file_id"]
        file_type = "document"
        mime_type = document.get("mime_type", "application/octet-stream")
        tg_path, filename = _get_file_path(token, file_id)
        # Prefer Telegram's file_name field (preserves original name)
        if document.get("file_name"):
            filename = document["file_name"]
    else:
        return

    data = _download_file(token, tg_path)
    rel_path = _save_inbox_file(data, filename, caption, file_type, mime_type, ts, from_)
    print(f"inbox: file saved — {rel_path} ({len(data)} bytes, caption={caption!r})", file=sys.stderr)


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
        # --- STATE-TRANSITION SIGNAL CHECK ---
        # Runs before every poll cycle. Structural guarantee: the agent cannot
        # receive a Telegram message without the watcher having first checked for
        # signals. Detection happens in deterministic Python, not LLM attention.
        # The watcher does NOT delete the signal file — only the agent does, after
        # writing checkpoint.json. This keeps signal delivery idempotent: if the
        # agent crashes mid-exit, the next watcher relaunch re-emits the signal.
        _signal_file = PROJECT_DIR / "state" / "conversation" / "reset_signal.txt"
        if _signal_file.exists():
            try:
                _sig_data = json.loads(_signal_file.read_text().strip())
                _sig_out = {
                    "event": "signal",
                    "action": _sig_data.get("action", "unknown"),
                    "reason": _sig_data.get("reason", ""),
                    "ts": _sig_data.get("ts", ""),
                }
                print(json.dumps(_sig_out))
                return 0
            except (json.JSONDecodeError, OSError) as _e:
                # Corrupt signal file — log and continue polling (don't crash).
                print(f"WARN: signal file unreadable: {_e}", file=sys.stderr)
        # --- END SIGNAL CHECK ---
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
                # Handle file messages (photo/document) — queue to inbox, keep polling
                if msg.get("photo") or msg.get("document"):
                    try:
                        handle_file_message(token, msg, allowed_chat)
                    except Exception as e:
                        print(f"file message error: {e}", file=sys.stderr)
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
                "event": "telegram_message",
                "update_id": update_id,
                "message_id": msg.get("message_id"),
                "chat_id": chat_id,
                "from": msg.get("from", {}).get("username", "unknown"),
                "text": text,
                "date": msg.get("date", 0),
            }
            print(json.dumps(out))
            return 0

        # Check outbox for pending execution-layer messages to forward
        forward_outbox()

        # No relevant updates in this batch — loop continues (long-poll)


if __name__ == "__main__":
    sys.exit(main())
