#!/usr/bin/env python3
"""
session_trigger_server.py — Manual session trigger endpoint.

Runs an HTTP server on port 8766 bound to 0.0.0.0 (Tailscale-accessible).
Receives POST /trigger with a secret token, launches a new agent session by
running wake.sh in the background, then waits briefly (up to
CONFIRM_TIMEOUT_SEC) for wake.sh to report whether the session actually
started -- wake.sh's own gates (already-running lock, philosophy cap) can
still decide not to launch anything, and the response reflects that instead
of just confirming a subprocess was spawned.

Auth: X-Trigger-Token header OR ?token=<value> query param.
Token is stored in state/trigger_token.txt.

iOS Shortcut: POST to http://100.110.36.84:8766/trigger
              Header: X-Trigger-Token: <value from trigger_token.txt>

Usage:
  python3 tools/session_trigger_server.py
"""

import http.server
import json
import logging
import os
import subprocess
import sys
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Optional

PORT = 8766
BIND_HOST = "0.0.0.0"

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent.parent
TOKEN_FILE = PROJECT_DIR / "state" / "trigger_token.txt"
WAKE_SH = PROJECT_DIR / "scripts" / "wake.sh"
PERSONA_FILE = PROJECT_DIR / "prompts" / "persona.txt"
RESULT_FILE = PROJECT_DIR / "state" / "manual_trigger_result.json"
CONFIRM_TIMEOUT_SEC = 8
CONFIRM_POLL_SEC = 0.25

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("trigger")


def load_token() -> str:
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip()
    return ""


def _wait_for_result(request_id: str, timeout: float = CONFIRM_TIMEOUT_SEC) -> Optional[dict]:
    """Poll RESULT_FILE for wake.sh's gate decision on this specific request.

    wake.sh writes this file (keyed by request_id) as soon as it knows whether
    a session actually launched -- well before the Claude session itself
    finishes, since that can run for hours. Without this, the only signal
    available here is "a subprocess was started," which is true even when a
    gate silently skips or aborts the launch a moment later.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if RESULT_FILE.exists():
            try:
                data = json.loads(RESULT_FILE.read_text())
                if data.get("request_id") == request_id:
                    return data
            except (json.JSONDecodeError, OSError):
                pass
        time.sleep(CONFIRM_POLL_SEC)
    return None


def fire_session() -> tuple:
    """Launch wake.sh in background, then confirm what actually happened.

    Returns (http_status_code, response_body_dict).
    """
    if not WAKE_SH.exists():
        return 500, {"ok": False, "error": f"wake.sh not found at {WAKE_SH}"}

    # Goal is resolved from Loom inside wake.sh itself now — no file to check
    # or fall back on here.
    persona = str(PERSONA_FILE) if PERSONA_FILE.exists() else ""

    cmd = ["bash", str(WAKE_SH)]
    if persona:
        cmd.append(persona)

    request_id = str(uuid.uuid4())
    env = os.environ.copy()
    env["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
    # Signal to wake.sh that this is a manual trigger — bypasses time window
    # (and, as of the break-glass change, the usage-limit gate too).
    env["TRIGGER_MODE"] = "manual"
    env["TRIGGER_REQUEST_ID"] = request_id

    try:
        subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as e:
        log.error("Failed to launch wake.sh: %s", e)
        return 500, {"ok": False, "error": f"failed to launch wake.sh: {e}"}

    log.info("Session trigger fired (request_id=%s): %s", request_id, " ".join(cmd))

    result = _wait_for_result(request_id)
    if result is None:
        return 202, {
            "ok": False,
            "started": "unknown",
            "reason": f"no confirmation from wake.sh within {CONFIRM_TIMEOUT_SEC}s "
                      "-- it may still be starting, or may have already finished its gates",
        }

    decision = result.get("decision")
    if decision == "launched":
        return 200, {
            "ok": True,
            "started": True,
            "session_type": result.get("session_type"),
            "pid": result.get("pid"),
        }
    if decision == "skipped":
        return 409, {"ok": False, "started": False, "reason": result.get("reason")}
    # decision == "aborted" (e.g. philosophy_cap), or any unrecognized value
    return 200, {"ok": False, "started": False, "reason": result.get("reason")}


class TriggerHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.info(fmt % args)

    def _send_json(self, code: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"status": "ok", "service": "session-trigger"})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/trigger":
            self._send_json(404, {"error": "not found"})
            return

        # Auth: check header first, then query param
        expected = load_token()
        if not expected:
            self._send_json(500, {"error": "trigger token not configured"})
            return

        token_header = self.headers.get("X-Trigger-Token", "")
        qs = urllib.parse.parse_qs(parsed.query)
        token_qs = qs.get("token", [""])[0]
        provided = token_header or token_qs

        if provided != expected:
            log.warning("Invalid token from %s", self.client_address[0])
            self._send_json(401, {"error": "invalid token"})
            return

        status_code, body = fire_session()
        self._send_json(status_code, body)


if __name__ == "__main__":
    token = load_token()
    if not token:
        log.warning("No trigger token found at %s — server will reject all requests", TOKEN_FILE)

    log.info("Session trigger server on %s:%d", BIND_HOST, PORT)
    log.info("Wake script: %s", WAKE_SH)
    log.info("Token file: %s", TOKEN_FILE)

    server = http.server.HTTPServer((BIND_HOST, PORT), TriggerHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Stopped")
