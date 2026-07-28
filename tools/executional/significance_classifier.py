#!/usr/bin/env python3
"""
significance_classifier.py — Heuristic significance classifier for night agent sessions.

Reads wake.log lines for a given session date prefix and identifies significant events.
Outputs structured JSON for use by consolidate_session.sh.

Usage:
    python3 significance_classifier.py --session 2026-07-04_em1
    python3 significance_classifier.py --date 2026-07-04 --all

Output (stdout): JSON with significant events and summary.

Part of Goal 4 — Memory and Continuity framework.
Written: 2026-07-04, @Lain
"""

import sys
import re
import json
import argparse
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.parent.parent

# --- Pattern definitions ---

TIER_1_PATTERNS = [
    # Owner communication — positive signals only
    (r'NEW.*GITHUB.*COMMENTS|NEW MESSAGES FOUND', 'owner_responded'),
    (r'NEW_COMMENT.*user=(andrii|owner)', 'owner_responded'),
    # Owner replied — various session log formats (prose and structured)
    (r'Received owner repl|owner repl.*wired|owner.*answered|owner.*responded', 'owner_responded'),
    (r'owner.*messages found|messages.*found.*owner|found.*owner.*messages', 'owner_responded'),
    (r'Andrii.*replied|Andrii.*approved|andrii.*approved', 'owner_responded'),
    (r'design report approved|architecture approved|plan approved', 'owner_responded'),
    # Telegram sends (machine output AND prose format)
    (r'sent message_id=\d+', 'telegram_sent'),
    (r'Sent Telegram msg \d+|Telegram msg \d+ sent|telegram.*msg \d+', 'telegram_sent'),
    # GitHub posts: machine output "comment_id=NNN"
    (r'comment_id=\d+', 'github_posted'),
    # GitHub posts: markdown session log — various formats
    (r'wired.*comment \d{7,}|Posted.*wired|wired.*Posted', 'github_posted'),
    (r'PR.*created|pull request.*created|PR.*opened|opened.*PR', 'github_posted'),
    (r'repo.*created|created.*repo|wired.*created|created.*wired', 'github_posted'),

    # Failures — specific patterns only
    (r'exit_code=[1-9]\d*\b', 'failure'),
    (r'\bFAILED\b.*tests|\btests.*\bFAILED\b', 'tests_failed'),
    (r'PAT.*401|token.*401|github.*401|token.*expired', 'auth_failure'),
    (r'ABORT: subscription usage too high|usage limit exceeded', 'usage_limit_hit'),

    # Milestones — specific check mark or explicit complete label
    (r'COMPLETE ✓|ALL PHASES.*DONE|all.*phases.*complete', 'milestone_complete'),
    (r'\d+/\d+ passing.*✓|\d+/\d+ tests.*green', 'milestone_complete'),

    # Deployment confirmed
    (r'running on \d{1,3}\.\d{1,3}\.\d{1,3}|deployed.*\d{1,3}\.\d{1,3}', 'deployed'),
]

TIER_2_PATTERNS = [
    # File creation (from Claude tool output — very specific message)
    (r'File created successfully at:', 'file_created'),

    # Git operations (machine and prose formats)
    (r'\bcommit\b.*[0-9a-f]{7,}|[0-9a-f]{7,}.*pushed|git push.*success', 'git_push'),
    (r'GitHub push.*success|push.*master:main|push.*main.*success', 'git_push'),
    (r'Pushed.*GitHub Contents API|pushed.*via.*API|pushed.*to.*loom|pushed.*to.*node', 'git_push'),

    # Tests passing (various formats found in session logs)
    (r'\d+/\d+ (?:tests )?pass(?:ing)?|all \d+ tests.*pass|\d+ passing', 'tests_passed'),
    (r'all \d+ tests.*green|\d+/\d+.*green', 'tests_passed'),

    # Phase completions in session logs (prose format)
    (r'Phase [A-E1-9]\b.*(?:complete|done|pushed|delivered)', 'phase_complete'),

    # System changes
    (r'systemctl.*enable|\.service.*enable|\.timer.*enable', 'service_change'),

    # Significant new tools/capabilities (tool output and prose formats)
    (r'File created successfully at:.*tools/', 'tool_created'),
    (r'[Bb]uilt tools/|[Nn]ew tools/|[Cc]reated tools/|new.*script.*creat', 'tool_created'),

    # LOOM task updates in session prose
    (r'LOOM.*task.*→|task.*→.*done|task.*→.*in_progress|loom.*task.*updat', 'task_management'),
]

IGNORE_PATTERNS = [
    # Routine orientation
    r'Reading.*memory|Loading.*memory|cat.*\.md',
    r'minutes remaining|context_pct',
    r'sessions_tonight|session_start_epoch',
    r'^$',  # empty lines
    # Routine wake infrastructure (not significant events)
    r'WARNING: could not check usage limits',
    r'LOOM context snapshot written',
    r'Dynamic goal text built from Loom',
    r'pre-incrementing session counter',
    r'wake\.sh.*starting|starting.*wake\.sh',
    r'Checking session count|session count.*ok',
    # Negative results (not significant)
    r'no new messages|no new comments|no new.*telegram',
    # Session end lines from wake.log (exit_code=0 is success)
    r'Session #\d+ ended\.',
    # Orientation steps (routine)
    r'Oriented:|checked time|session count.*ok|context.*%',
    r'HOT STATE|read.*memory files|memory files.*read',
    # Routine memory writes at session end
    r'Updated latest_summary|Updated.*state.*last_comment|Updated andrii\.md',
]


def should_ignore(line: str) -> bool:
    """Return True if this line is routine and should be filtered out."""
    for pattern in IGNORE_PATTERNS:
        if re.search(pattern, line, re.IGNORECASE):
            return True
    return False


def classify_line(line: str) -> dict | None:
    """
    Classify a single log line.
    Returns dict with tier, category, text; or None if not significant.
    """
    if should_ignore(line):
        return None

    # Check Tier 1 first
    for pattern, category in TIER_1_PATTERNS:
        if re.search(pattern, line, re.IGNORECASE):
            return {'tier': 1, 'category': category, 'text': line.strip()}

    # Then Tier 2
    for pattern, category in TIER_2_PATTERNS:
        if re.search(pattern, line, re.IGNORECASE):
            return {'tier': 2, 'category': category, 'text': line.strip()}

    return None


def read_wake_log(session_prefix: str | None = None) -> list[str]:
    """Read wake.log, optionally filtered to a session prefix."""
    log_path = PROJECT_DIR / 'logs' / 'wake.log'
    if not log_path.exists():
        return []

    lines = log_path.read_text(encoding='utf-8').splitlines()

    if session_prefix:
        # Filter to lines containing the session date
        # Wake log format: "2026-07-04_em1 TYPE — description"
        filtered = [l for l in lines if session_prefix in l]
        if not filtered:
            # Try just date prefix
            date_part = session_prefix.split('_')[0]
            filtered = [l for l in lines if date_part in l]
        return filtered

    return lines


def read_session_log(session_id: str) -> str | None:
    """Read the session markdown log if it exists."""
    # Try various name formats
    log_path = PROJECT_DIR / 'memory' / 'sessions' / f'{session_id}.md'
    if log_path.exists():
        return log_path.read_text(encoding='utf-8')
    return None


def scan_session_log_for_events(session_log: str) -> list[dict]:
    """
    Scan a session's markdown log for additional significant events
    not captured in wake.log.
    """
    if not session_log:
        return []

    events = []
    for line in session_log.splitlines():
        result = classify_line(line)
        if result:
            events.append(result)

    return events


def classify_session(session_id: str) -> dict:
    """
    Run full significance classification for a session.
    Returns structured result with events, summary, and flags.
    """
    # Read and classify wake.log lines
    wake_lines = read_wake_log(session_id)
    events = []
    for line in wake_lines:
        result = classify_line(line)
        if result:
            events.append(result)

    # Also scan the session markdown log if available
    session_log = read_session_log(session_id)
    if session_log:
        session_events = scan_session_log_for_events(session_log)
        # Deduplicate by text
        existing_texts = {e['text'] for e in events}
        for e in session_events:
            if e['text'] not in existing_texts:
                events.append(e)
                existing_texts.add(e['text'])

    # Build summary
    tier1 = [e for e in events if e['tier'] == 1]
    tier2 = [e for e in events if e['tier'] == 2]

    owner_events = [e for e in tier1 if e['category'] == 'owner_responded']
    auth_failures = [e for e in tier1 if e['category'] == 'auth_failure']
    failures = [e for e in tier1 if e['category'] == 'failure']
    milestones = [e for e in tier1 + tier2 if e['category'] in ('milestone_complete', 'phase_complete', 'deployed')]
    files_created = [e for e in tier2 if e['category'] == 'file_created']

    # Flags for memory writing
    flags = {
        'owner_responded': len(owner_events) > 0,
        'review_andrii_md': len(owner_events) > 0,
        'narrative_update_recommended': len(tier1) > 0,
        'has_failure': len(failures) > 0 or len(auth_failures) > 0,
        'has_milestone': len(milestones) > 0,
        'files_created_count': len(files_created),
    }

    return {
        'session_id': session_id,
        'events': events,
        'tier1_count': len(tier1),
        'tier2_count': len(tier2),
        'total_significant': len(events),
        'flags': flags,
        'tier1_categories': list({e['category'] for e in tier1}),
        'tier2_categories': list({e['category'] for e in tier2}),
    }


def format_report(result: dict) -> str:
    """Format classification result as human-readable report."""
    lines = []
    lines.append(f"=== SIGNIFICANCE REPORT: {result['session_id']} ===")
    lines.append("")

    tier1_events = [e for e in result['events'] if e['tier'] == 1]
    tier2_events = [e for e in result['events'] if e['tier'] == 2]

    if tier1_events:
        lines.append("TIER 1 (high significance):")
        for e in tier1_events:
            lines.append(f"  [{e['category']}] {e['text'][:100]}")
        lines.append("")

    if tier2_events:
        lines.append("TIER 2 (medium significance):")
        for e in tier2_events:
            lines.append(f"  [{e['category']}] {e['text'][:100]}")
        lines.append("")

    lines.append("=== SUMMARY ===")
    lines.append(f"Total significant events: {result['total_significant']} "
                 f"({result['tier1_count']} T1, {result['tier2_count']} T2)")
    lines.append("")

    flags = result['flags']
    if flags['owner_responded']:
        lines.append("  >> Owner responded this session — review andrii.md")
    if flags['has_failure']:
        lines.append("  >> Failures detected — note in learnings.md")
    if flags['has_milestone']:
        lines.append("  >> Milestone(s) completed — update progress.md")
    if flags['narrative_update_recommended']:
        lines.append("  >> Narrative update recommended (Tier 1 events present)")
    else:
        lines.append("  >> Narrative update optional (no Tier 1 events)")

    if flags['files_created_count'] > 0:
        lines.append(f"  >> {flags['files_created_count']} new file(s) created — update index.md")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description='Significance classifier for night agent sessions'
    )
    parser.add_argument('--session', type=str,
                        help='Session ID to classify (e.g., 2026-07-04_em1)')
    parser.add_argument('--json', action='store_true',
                        help='Output raw JSON instead of formatted report')
    parser.add_argument('--date', type=str,
                        help='Date prefix (YYYY-MM-DD) to classify all sessions from that date')

    args = parser.parse_args()

    if not args.session and not args.date:
        print("ERROR: --session or --date required", file=sys.stderr)
        sys.exit(1)

    session_id = args.session or args.date

    result = classify_session(session_id)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(format_report(result))


if __name__ == '__main__':
    main()
