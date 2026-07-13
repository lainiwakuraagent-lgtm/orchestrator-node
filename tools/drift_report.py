#!/usr/bin/env python3
"""
drift_report.py — Compare agent_project against blank_node harness for divergence.

Usage:
    python3 tools/drift_report.py [--send] [--blank-node PATH] [--project PATH]

Exit codes:
    0 — no drift (or only expected drift)
    1 — drift detected
    2 — error
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

# Files to track for harness-level drift.
# These exist in both blank_node and agent_project and should stay in sync.
TRACKED_FILES = [
    "scripts/wake.sh",
    "scripts/resolve_session_type.py",
    "tools/analytics_write.py",
    "tools/relationship_update.py",
    "tools/behavioral_adapter.py",
    "tools/command_dispatcher.py",
    "tools/check_nexus.sh",
    "tools/check_replies.sh",
    "tools/telegram_send.sh",
    "tools/inbox_startup.py",
    "tools/inbox_append.py",
    "tools/inbox_read.py",
    "tools/session_trigger_server.py",
    "tools/owner_brief.py",
]

# Files in agent_project but NOT expected in blank_node (instance-specific)
INSTANCE_ONLY = {
    "identity/credentials.md",
    "state/nexus_lain_token.txt",
    "memory/",
    "logs/",
    "prompts/goal.txt",
    "prompts/persona.txt",
}


def run_diff(file_a: Path, file_b: Path) -> tuple[int, str]:
    """Return (changed_lines, unified_diff_excerpt) for two files."""
    try:
        result = subprocess.run(
            ["diff", "-u", str(file_a), str(file_b)],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return 0, ""
        # Count changed lines (lines starting with + or - but not ---)
        diff_text = result.stdout
        changed = sum(
            1 for line in diff_text.splitlines()
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
        )
        # Return a short excerpt (first 20 diff lines)
        excerpt_lines = [
            line for line in diff_text.splitlines()
            if not line.startswith(("---", "+++", "@@"))
        ][:20]
        excerpt = "\n".join(excerpt_lines)
        return changed, excerpt
    except FileNotFoundError:
        return -1, "diff command not found"


def build_report(blank_node: Path, project: Path) -> tuple[bool, str]:
    """Build a drift report. Returns (has_drift, markdown_report)."""
    lines = ["# Drift Report: agent_project vs blank_node", ""]
    lines.append(f"- blank_node: `{blank_node}`")
    lines.append(f"- agent_project: `{project}`")
    lines.append("")

    drifted = []
    missing_in_blank = []
    missing_in_project = []

    for rel in TRACKED_FILES:
        a = blank_node / rel
        b = project / rel

        if not a.exists() and not b.exists():
            continue
        elif not a.exists():
            missing_in_blank.append(rel)
        elif not b.exists():
            missing_in_project.append(rel)
        else:
            changed, excerpt = run_diff(a, b)
            if changed > 0:
                drifted.append((rel, changed, excerpt))

    has_drift = bool(drifted or missing_in_blank or missing_in_project)

    if not has_drift:
        lines.append("✓ No drift detected in tracked files.")
        return False, "\n".join(lines)

    if drifted:
        lines.append(f"## Files with drift ({len(drifted)})")
        lines.append("")
        for rel, changed, excerpt in drifted:
            lines.append(f"### `{rel}` — {changed} changed lines")
            if excerpt:
                lines.append("```diff")
                lines.append(excerpt)
                lines.append("```")
            lines.append("")

    if missing_in_blank:
        lines.append(f"## In agent_project but NOT in blank_node ({len(missing_in_blank)})")
        for rel in missing_in_blank:
            lines.append(f"- `{rel}`")
        lines.append("")

    if missing_in_project:
        lines.append(f"## In blank_node but NOT in agent_project ({len(missing_in_project)})")
        for rel in missing_in_project:
            lines.append(f"- `{rel}`")
        lines.append("")

    lines.append(
        "**Action:** Review drifted files. Backport improvements to blank_node before next node deployment."
        " PAT must be valid to push."
    )

    return True, "\n".join(lines)


def send_telegram(message: str, project: Path) -> None:
    send_sh = project / "tools" / "telegram_send.sh"
    if not send_sh.exists():
        print(f"[drift_report] telegram_send.sh not found at {send_sh}", file=sys.stderr)
        return
    subprocess.run(
        ["bash", str(send_sh)],
        input=message.encode(),
        check=False,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Detect drift between agent_project and blank_node")
    parser.add_argument("--blank-node", default="/home/andrii/lain/blank_node",
                        help="Path to blank_node harness checkout")
    parser.add_argument("--project", default=str(Path(__file__).resolve().parent.parent),
                        help="Path to agent_project instance")
    parser.add_argument("--send", action="store_true",
                        help="Send result via Telegram")
    parser.add_argument("--quiet", action="store_true",
                        help="Only print if drift detected")
    args = parser.parse_args()

    blank_node = Path(args.blank_node)
    project = Path(args.project)

    if not blank_node.exists():
        print(f"[drift_report] blank_node not found at {blank_node}", file=sys.stderr)
        return 2
    if not project.exists():
        print(f"[drift_report] project not found at {project}", file=sys.stderr)
        return 2

    has_drift, report = build_report(blank_node, project)

    if not args.quiet or has_drift:
        print(report)

    if args.send and has_drift:
        # Send a compact version for Telegram
        summary_lines = ["@Lain drift report:"]
        for line in report.splitlines():
            if line.startswith("### `") or line.startswith("- `") or line.startswith("**Action"):
                summary_lines.append(line.replace("```diff", "").replace("```", ""))
        send_telegram("\n".join(summary_lines), project)

    return 1 if has_drift else 0


if __name__ == "__main__":
    sys.exit(main())
