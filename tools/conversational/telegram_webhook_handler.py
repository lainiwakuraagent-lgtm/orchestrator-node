#!/usr/bin/env python3
"""
telegram_webhook_handler.py — Minimal Telegram webhook receiver.

Runs an HTTP server on port 8765 that accepts Telegram webhook POSTs.
Incoming messages are written to state/telegram_incoming.txt, one per line:
  YYYY-MM-DD HH:MM | @username | message text

check_replies.sh reads and clears this file at session start.

Security: Accepts all POST /webhook requests (Telegram IPs only in production,
but since this is inside Tailscale funnel, no additional filtering needed).
"""

import http.server
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

PORT = 8765
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent.parent
INCOMING_FILE = PROJECT_DIR / "state" / "telegram_incoming.txt"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("webhook")


class WebhookHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.info(fmt % args)

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path != "/webhook":
            self.send_response(404)
            self.end_headers()
            return

        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len)

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            log.warning("Non-JSON POST received")
            self.send_response(400)
            self.end_headers()
            return

        # Extract message fields
        msg = data.get("message") or data.get("channel_post") or {}
        text = msg.get("text", "")
        sender = msg.get("from", {}).get("username", "unknown")
        chat_id = msg.get("chat", {}).get("id", "?")
        ts = msg.get("date", 0)

        if text:
            dt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
            line = f"{dt} | @{sender} | chat={chat_id} | {text}\n"
            INCOMING_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(INCOMING_FILE, "a") as f:
                f.write(line)
            log.info("Stored message from @%s: %s", sender, text[:80])

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")


if __name__ == "__main__":
    log.info("Starting Telegram webhook handler on port %d", PORT)
    log.info("Writing incoming messages to %s", INCOMING_FILE)

    server = http.server.HTTPServer(("127.0.0.1", PORT), WebhookHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Stopped")
