#!/usr/bin/env python3
"""
session_digest.py — Compact narrative digest of recent sessions.

Usage:
  python3 tools/session_digest.py [--last N] [--since DATE] [--send] [--files-only]

Output: Chronological summary of N most recent session logs, including:
  - Session ID + type
  - What was done (subsection headers or first paragraph)
  - Key decisions (up to 3 bullets)
  - Exit reason (first line)

Flags:
  --last N        Number of most recent sessions (default: 7)
  --since DATE    Sessions since YYYY-MM-DD (overrides --last)
  --send          Send result to Telegram via tools/telegram_send.sh
  --files-only    Print matched session filenames only (debugging)
"""

import os
import re
import sys
import argparse
import subprocess
from pathlib import Path

PROJECT_DIR = Path(os.environ.get("PROJECT_DIR", Path(__file__).parent.parent.parent))
SESSIONS_DIR = PROJECT_DIR / "memory" / "sessions"
SEND_SCRIPT = PROJECT_DIR / "tools" / "telegram_send.sh"


def _load_agent_tag() -> str:
    """Read AGENT_NAME from state/agent_config.env, default 'agent'."""
    config_path = PROJECT_DIR / "state" / "agent_config.env"
    name = "agent"
    if config_path.exists():
        for line in config_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("AGENT_NAME="):
                name = line.split("=", 1)[1].strip()
                break
    return "@" + name[:1].upper() + name[1:]


AGENT_TAG = _load_agent_tag()


def parse_session_file(path: Path) -> dict:
    """Extract key fields from a session log file."""
    try:
        text = path.read_text(errors="replace")
    except OSError as e:
        return {"id": path.stem, "error": str(e)}

    result = {"id": path.stem}

    # Header fields (from bold key: value lines)
    for field, pattern in [
        ("type",        r"\*\*Type:\*\*\s*(.+)"),
        ("mode",        r"\*\*Mode:\*\*\s*(.+)"),
        ("context_pct", r"\*\*Context at exit:\*\*\s*(.+)"),
        ("commits",     r"\*\*Commits pushed:\*\*\s*(.+)"),
    ]:
        m = re.search(pattern, text)
        result[field] = m.group(1).strip() if m else ""

    # Session type note (first paragraph after "## Session type chosen")
    m = re.search(r"## Session type chosen[^\n]*\n+(.+?)(?:\n\n|\n##)", text, re.DOTALL)
    if m:
        first = m.group(1).strip().replace("\n", " ")
        result["type_note"] = first[:200]
    else:
        result["type_note"] = ""

    # What was done — prefer ### subsection names, fallback to first paragraph
    what_m = re.search(r"## What was done\n(.*?)(?:\n## |\Z)", text, re.DOTALL)
    if what_m:
        body = what_m.group(1)
        subsections = re.findall(r"^### (.+)$", body, re.MULTILINE)
        if subsections:
            result["what_done"] = " · ".join(subsections)
        else:
            paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
            result["what_done"] = paragraphs[0].replace("\n", " ")[:300] if paragraphs else ""
    else:
        result["what_done"] = ""

    # Key decisions — up to 3 bullet points
    dec_m = re.search(r"## Key decisions\n(.*?)(?:\n## |\Z)", text, re.DOTALL)
    if dec_m:
        bullets = re.findall(r"^- (.+)$", dec_m.group(1), re.MULTILINE)
        result["key_decisions"] = [b.strip() for b in bullets[:3]]
    else:
        result["key_decisions"] = []

    # Exit reason — first non-empty line of that section
    exit_m = re.search(r"## Exit reason\n+(.*?)(?:\n\n|\n##|\Z)", text, re.DOTALL)
    if exit_m:
        lines = [l.strip() for l in exit_m.group(1).split("\n") if l.strip()]
        # Skip kaomoji-only lines
        text_lines = [l for l in lines if re.search(r"[a-zA-Z]", l)]
        result["exit_reason"] = text_lines[0][:150] if text_lines else (lines[0][:150] if lines else "")
    else:
        result["exit_reason"] = ""

    return result


def format_session(s: dict) -> str:
    """Format a single session as a compact text block."""
    if "error" in s:
        return f"◈ {s['id']} [ERROR: {s['error']}]"

    header = f"◈ {s['id']}"
    if s.get("type"):
        header += f"  [{s['type']}]"
    if s.get("mode"):
        header += f"  {s['mode']}"

    lines = [header]

    if s.get("what_done"):
        lines.append(f"  {s['what_done']}")

    for d in s.get("key_decisions", []):
        lines.append(f"  · {d}")

    if s.get("exit_reason"):
        lines.append(f"  → {s['exit_reason']}")

    if s.get("commits") and s["commits"].strip():
        lines.append(f"  git: {s['commits']}")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Compact digest of recent agent session logs"
    )
    parser.add_argument(
        "--last", type=int, default=7,
        help="Number of most recent sessions to include (default: 7)"
    )
    parser.add_argument(
        "--since", type=str, default=None,
        help="Only sessions with filenames >= YYYY-MM-DD"
    )
    parser.add_argument(
        "--send", action="store_true",
        help="Send digest to Telegram via tools/telegram_send.sh"
    )
    parser.add_argument(
        "--files-only", action="store_true",
        help="Print matched filenames and exit (debugging)"
    )
    args = parser.parse_args()

    if not SESSIONS_DIR.exists():
        print(f"ERROR: sessions dir not found: {SESSIONS_DIR}", file=sys.stderr)
        sys.exit(1)

    files = sorted(SESSIONS_DIR.glob("*.md"))

    if args.since:
        files = [f for f in files if f.stem >= args.since]
    else:
        files = files[-args.last:]

    if args.files_only:
        for f in files:
            print(f.name)
        return

    if not files:
        print("No sessions found.")
        return

    sessions = [parse_session_file(f) for f in files]

    header = f"{AGENT_TAG} — Session Digest ({len(sessions)} sessions)"
    separator = "─" * len(header)
    blocks = [header, separator, ""]

    for s in sessions:
        blocks.append(format_session(s))
        blocks.append("")

    output = "\n".join(blocks).rstrip()

    if args.send:
        proc = subprocess.run(
            ["bash", str(SEND_SCRIPT)],
            input=output,
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            print("Digest sent to Telegram.")
        else:
            print(f"Send failed (exit {proc.returncode}): {proc.stderr.strip()}", file=sys.stderr)
            print(output)
    else:
        print(output)


if __name__ == "__main__":
    main()
