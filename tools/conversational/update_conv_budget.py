#!/usr/bin/env python3
"""update_conv_budget.py — Update state/conversation/context_budget.json.

Called by the conversational agent after each message exchange.
Runs check_session.sh --context to get current context %, increments message
counters, and writes the result to context_budget.json for /context command to read.

Usage:
    python3 tools/update_conv_budget.py [--recv] [--sent]

Flags:
    --recv   increment messages_received counter (default: increment both)
    --sent   increment messages_sent counter (default: increment both)

If no flags given, increments both (standard per-exchange call).
"""
import json
import re
import subprocess
import sys
import time
import pathlib

PROJECT_DIR = pathlib.Path(__file__).parent.parent.parent
BUDGET_FILE = PROJECT_DIR / "state" / "conversation" / "context_budget.json"
CHECK_SESSION = PROJECT_DIR / "tools" / "check_session.sh"


def get_context_pct() -> int:
    try:
        result = subprocess.run(
            ["bash", str(CHECK_SESSION), "--context"],
            capture_output=True, text=True, timeout=20
        )
        m = re.search(r'context_pct_estimate:\s*(\d+)%', result.stdout)
        return int(m.group(1)) if m else 0
    except Exception:
        return 0


def load_budget() -> dict:
    if BUDGET_FILE.exists():
        try:
            return json.loads(BUDGET_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "session_start": int(time.time()),
        "messages_sent": 0,
        "messages_received": 0,
        "estimated_context_pct": 0,
    }


def main():
    args = set(sys.argv[1:])
    inc_recv = "--recv" in args or len(args) == 0
    inc_sent = "--sent" in args or len(args) == 0

    data = load_budget()
    pct = get_context_pct()

    data["estimated_context_pct"] = pct
    data["last_updated"] = int(time.time())
    if inc_recv:
        data["messages_received"] = data.get("messages_received", 0) + 1
    if inc_sent:
        data["messages_sent"] = data.get("messages_sent", 0) + 1

    BUDGET_FILE.parent.mkdir(parents=True, exist_ok=True)
    BUDGET_FILE.write_text(json.dumps(data, indent=2))
    print(f"context_budget: {pct}% | sent={data['messages_sent']} recv={data['messages_received']}")


if __name__ == "__main__":
    main()
