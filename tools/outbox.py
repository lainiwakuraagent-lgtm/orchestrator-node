#!/usr/bin/env python3
"""
outbox.py — Unified outbox tool.

Subcommands:
  send [--content TEXT] [--from SENDER] [--type message|question] [--to DEST]
       [--priority normal|high] [--check]
      Write a typed entry to state/conversation/outbox.json.
      Reads content from stdin if --content is not provided.
      Use --check to print pending count without writing.

  drain [--dry-run]
      Route pending entries via state/delivery_routing.json.
      Intended to be called by a systemd timer or the conversational layer.

  check
      Print pending count (shortcut for send --check).

Outbox schema:
  {
    "id": "<8-char uuid>",
    "from": "<sender>",
    "type": "message" | "question",
    "to": "owner" | "agent:<name>",
    "content": "<text>",
    "timestamp": <unix_ts>,
    "sent": false,
    "priority": "normal" | "high"   (optional)
  }

Routing (done by drain):
  type=message  + to=owner   -> Telegram
  type=question + to=owner   -> Telegram (prefixed "Question for you:")

A question-type send() also appends to state/conversation/open_questions.json
(status=open). The conversational layer marks entries answered in-session.
Entries still open at idle-close are left as-is -- no automatic escalation.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
OUTBOX_FILE = PROJECT_DIR / "state" / "conversation" / "outbox.json"
OPEN_QUESTIONS_FILE = PROJECT_DIR / "state" / "conversation" / "open_questions.json"
ROUTING_FILE = PROJECT_DIR / "state" / "delivery_routing.json"
WATCHER_QUEUE_FILE = PROJECT_DIR / "state" / "nexus_watcher_queue.json"
WAKE_LOG = PROJECT_DIR / "logs" / "wake.log"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def load_outbox() -> list:
    OUTBOX_FILE.parent.mkdir(parents=True, exist_ok=True)
    if OUTBOX_FILE.exists():
        try:
            return json.loads(OUTBOX_FILE.read_text())
        except (json.JSONDecodeError, ValueError):
            return []
    return []


def save_outbox(entries: list) -> None:
    OUTBOX_FILE.write_text(json.dumps(entries, indent=2))


# ---------------------------------------------------------------------------
# send — queue a message
# ---------------------------------------------------------------------------

def send_message(
    content: str,
    sender: str = "execution_layer",
    msg_type: str = "message",
    to: str = "owner",
    priority: "str | None" = None,
) -> None:
    """Queue a message to the outbox. Importable by other modules."""
    entries = load_outbox()
    entry: dict = {
        "id": str(uuid.uuid4())[:8],
        "from": sender,
        "type": msg_type,
        "to": to,
        "content": content,
        "timestamp": int(time.time()),
        "sent": False,
    }
    if priority:
        entry["priority"] = priority
    entries.append(entry)
    save_outbox(entries)
    print(f"outbox: queued {len([e for e in entries if not e.get('sent')])} pending")

    if msg_type == "question":
        _append_open_question(content, to)


def _append_open_question(content: str, to: str) -> None:
    """Append a question to the open_questions ledger."""
    entries = []
    if OPEN_QUESTIONS_FILE.exists():
        try:
            entries = json.loads(OPEN_QUESTIONS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            entries = []
    entry = {
        "id": str(uuid.uuid4())[:8],
        "content": content,
        "to": to,
        "sent_at": int(time.time()),
        "status": "open",
    }
    entries.append(entry)
    OPEN_QUESTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    OPEN_QUESTIONS_FILE.write_text(json.dumps(entries, indent=2))


def check_pending() -> int:
    entries = load_outbox()
    pending = [e for e in entries if not e.get("sent")]
    print(f"outbox: {len(pending)} pending, {len(entries)} total")
    return len(pending)


def main_send() -> int:
    parser = argparse.ArgumentParser(description="Write to execution outbox")
    parser.add_argument("--content", "-c", help="Message content (or read from stdin)")
    parser.add_argument("--from", dest="sender", default="execution_layer",
                        help="Sender label (default: execution_layer)")
    parser.add_argument("--type", dest="msg_type", default="message",
                        choices=["message", "question"],
                        help="Entry type: message (default) or question")
    parser.add_argument("--to", dest="to", default="owner",
                        help="Destination: 'owner' (default) or 'agent:<name>'")
    parser.add_argument("--priority", default=None,
                        choices=["normal", "high"],
                        help="Optional priority flag")
    parser.add_argument("--check", action="store_true",
                        help="Check pending count only, do not write")
    args = parser.parse_args()

    if args.check:
        check_pending()
        return 0

    content = args.content
    if not content:
        if not sys.stdin.isatty():
            content = sys.stdin.read().strip()
        else:
            print("ERROR: provide --content or pipe message via stdin", file=sys.stderr)
            return 1

    if not content:
        print("ERROR: empty message", file=sys.stderr)
        return 1

    send_message(content, sender=args.sender, msg_type=args.msg_type,
                 to=args.to, priority=args.priority)
    return 0


# ---------------------------------------------------------------------------
# drain — route pending entries
# ---------------------------------------------------------------------------

def _log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] outbox_drain: {msg}"
    print(line, file=sys.stderr)
    try:
        WAKE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(WAKE_LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_routing() -> dict:
    if not ROUTING_FILE.exists():
        _log(f"routing file missing: {ROUTING_FILE}")
        return {}
    try:
        return json.loads(ROUTING_FILE.read_text())
    except (json.JSONDecodeError, OSError) as e:
        _log(f"routing load error: {e}")
        return {}


def _send_telegram(route: dict, content: str) -> bool:
    """Send content via Telegram. route must contain a 'chat_id' key."""
    chat_id = route.get("chat_id", "")
    try:
        env = {**os.environ, "SKIP_TTS": "1", "TELEGRAM_CHAT_ID": chat_id}
        proc = subprocess.run(
            ["bash", str(SCRIPT_DIR / "telegram_send.sh")],
            input=content, text=True, capture_output=True, timeout=35, env=env,
        )
        if proc.returncode != 0:
            _log(f"telegram send failed (rc={proc.returncode}): {proc.stderr[:200]}")
            return False
        return True
    except Exception as e:
        _log(f"telegram send error: {e}")
        return False


SENDERS = {"telegram": _send_telegram}


def _route_nexus_send(entry: dict, dry_run: bool = False) -> bool:
    """Route a nexus_send entry to state/nexus_watcher_queue.json.

    nexus_watcher.py (always-on process) polls this file and sends via Nexus API.
    nexus_send schema: {type:"nexus_send", channel_id:<uuid>, content:<str>,
                        expects_reply:<bool>}
    """
    if dry_run:
        _log(f"[DRY-RUN] would queue nexus_send for channel {entry.get('channel_id') or entry.get('channel')}")
        return True
    try:
        existing: list = []
        if WATCHER_QUEUE_FILE.exists():
            try:
                existing = json.loads(WATCHER_QUEUE_FILE.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        existing.append({
            "id": entry.get("id", f"ns_{int(time.time())}"),
            "channel_id": entry.get("channel_id") or entry.get("channel"),
            "content": entry.get("content", ""),
            "expects_reply": entry.get("expects_reply", False),
            "queued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "sent": False,
        })
        WATCHER_QUEUE_FILE.write_text(json.dumps(existing, indent=2))
        _log(f"queued nexus_send to watcher_queue (channel={entry.get('channel_id') or entry.get('channel')})")
        return True
    except OSError as e:
        _log(f"watcher queue write error: {e}")
        return False


def _drain(dry_run: bool = False) -> int:
    if not OUTBOX_FILE.exists():
        return 0
    try:
        entries = json.loads(OUTBOX_FILE.read_text())
    except (json.JSONDecodeError, OSError) as e:
        _log(f"outbox load error: {e}")
        return 0

    routing = load_routing()
    if not routing:
        return 0

    sent_count, changed = 0, False
    for entry in entries:
        if entry.get("sent"):
            continue
        content = entry.get("content", "").strip()
        if not content:
            entry["sent"] = True
            changed = True
            continue

        msg_type = entry.get("type", "message")

        # nexus_send entries go to nexus_watcher_queue.json, not sent directly.
        if msg_type == "nexus_send":
            if _route_nexus_send(entry, dry_run=dry_run):
                entry["sent"] = True
                changed = True
                sent_count += 1
            continue

        to = entry.get("to", "owner")
        route_key = to if to in routing else "owner"
        route = routing.get(route_key)
        if not route:
            _log(f"no route for '{to}' (and no owner fallback)")
            continue

        channel = route.get("channel", "")
        if msg_type == "question":
            content = (
                f"Question for you:\n{content}" if channel == "telegram"
                else f"[question] {content}"
            )

        entry_id = entry.get("id", "?")
        if dry_run:
            _log(f"[DRY-RUN] would send {entry_id} -> {route_key} via {channel}")
            sent_count += 1
            continue

        sender_fn = SENDERS.get(channel)
        if not sender_fn:
            _log(f"unknown channel '{channel}' for route '{route_key}'")
            continue
        if sender_fn(route, content):
            entry["sent"] = True
            changed = True
            sent_count += 1
            _log(f"sent {entry_id} -> {route_key} via {channel}")

    if changed:
        try:
            OUTBOX_FILE.write_text(json.dumps(entries, indent=2))
        except OSError as e:
            _log(f"outbox write error: {e}")
    return sent_count


def main_drain() -> int:
    parser = argparse.ArgumentParser(description="Drain outbox")
    parser.add_argument("--dry-run", action="store_true", help="Log without sending")
    args = parser.parse_args()

    pending = 0
    if OUTBOX_FILE.exists():
        try:
            entries = json.loads(OUTBOX_FILE.read_text())
            pending = sum(1 for e in entries if not e.get("sent"))
        except (json.JSONDecodeError, OSError):
            pass
    if pending == 0:
        return 0

    _log(f"starting drain -- {pending} pending")
    sent = _drain(dry_run=args.dry_run)
    if sent:
        _log(f"drain complete -- sent {sent}/{pending}")
    return 0


# ---------------------------------------------------------------------------
# check — pending count shortcut
# ---------------------------------------------------------------------------

def main_check() -> int:
    check_pending()
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: outbox.py <send|drain|check> [args]", file=sys.stderr)
        sys.exit(1)
    cmd = sys.argv.pop(1)
    if cmd == "send":
        sys.exit(main_send())
    elif cmd == "drain":
        sys.exit(main_drain())
    elif cmd == "check":
        sys.exit(main_check())
    else:
        print(f"Unknown subcommand: {cmd}", file=sys.stderr)
        print("Usage: outbox.py <send|drain|check> [args]", file=sys.stderr)
        sys.exit(1)
