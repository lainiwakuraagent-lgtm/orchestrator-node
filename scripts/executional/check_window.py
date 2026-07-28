#!/usr/bin/env python3
"""
check_window.py
Gate 2 (nightly only) — window + trigger check against config/session_schedule.json.

Extracted from wake.sh, where this logic previously lived as an inline ~170-line
Python heredoc. Read-only by default: detecting a matching one_off entry does NOT
mark it fired here. wake.sh must call --mark-fired separately, after Gate 4 (the
session-lock check) has passed, so a one_off entry can never be consumed without
the session actually launching.

Usage:
  python3 scripts/check_window.py check --schedule-file PATH --lock-file PATH
  python3 scripts/check_window.py mark-fired --schedule-file PATH --one-off-index N

`check` prints (to stdout):
  LAUNCH: yes|no
  WINDOW_TYPE: work|maintenance|reflection|none
  one_off_index: N        (only present when decision reason is one_off_match)
  decision=<launch|abort|skip> reason=<...> [window=... | label=... | windows=...]
"""

import argparse
import json
import os
import sys
from datetime import datetime

TRIGGER_TOLERANCE_SEC = 45


def time_to_minutes(t: str) -> int:
    h, m = map(int, t.split(':'))
    return h * 60 + m


def time_to_seconds(t: str) -> int:
    h, m = map(int, t.split(':'))
    return h * 3600 + m * 60


def in_window(start_str: str, end_str: str, now_min: int) -> bool:
    start = time_to_minutes(start_str)
    end = time_to_minutes(end_str)
    if start <= end:
        return start <= now_min < end
    # wraps midnight (e.g., 23:00-05:00)
    return now_min >= start or now_min < end


def windows_overlap(a: dict, b: dict) -> bool:
    """Check if two windows overlap. Handles midnight wrapping."""
    a_start = time_to_minutes(a['start'])
    a_end = time_to_minutes(a['end'])
    b_start = time_to_minutes(b['start'])
    b_end = time_to_minutes(b['end'])

    def expand(s, e):
        if s <= e:
            return [(s, e)]
        return [(s, 1440), (0, e)]

    a_ranges = expand(a_start, a_end)
    b_ranges = expand(b_start, b_end)

    for ar in a_ranges:
        for br in b_ranges:
            if ar[0] < br[1] and br[0] < ar[1]:
                return True
    return False


def cmd_check(args: argparse.Namespace) -> int:
    schedule_file = args.schedule_file
    lock_file = args.lock_file

    try:
        with open(schedule_file) as f:
            schedule = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print('ERROR: cannot read schedule: ' + str(e), file=sys.stderr)
        print('LAUNCH: yes')
        print('WINDOW_TYPE: work')
        print('decision=launch reason=schedule_unreadable')
        return 0

    now = datetime.now()
    now_minutes = now.hour * 60 + now.minute
    now_seconds = now.hour * 3600 + now.minute * 60 + now.second

    enabled_windows = [w for w in schedule.get('windows', []) if w.get('enabled', True)]

    for i in range(len(enabled_windows)):
        for j in range(i + 1, len(enabled_windows)):
            if windows_overlap(enabled_windows[i], enabled_windows[j]):
                la = enabled_windows[i]['label']
                lb = enabled_windows[j]['label']
                print(f'ERROR: overlapping windows: {la} and {lb}', file=sys.stderr)
                print('LAUNCH: no')
                print('WINDOW_TYPE: none')
                print(f'decision=abort reason=overlapping_windows windows={la},{lb}')
                return 0

    matched_window = None
    matched_type = 'work'
    trigger_hit = False
    consecutive_run = False

    for w in enabled_windows:
        if not in_window(w['start'], w['end'], now_minutes):
            continue
        matched_window = w['label']
        matched_type = w.get('type', 'work')

        for trigger in w.get('triggers', []):
            trigger_sec = time_to_seconds(trigger)
            diff = abs(now_seconds - trigger_sec)
            if diff > 43200:
                diff = 86400 - diff
            if diff <= TRIGGER_TOLERANCE_SEC:
                trigger_hit = True
                break

        if not trigger_hit:
            if not os.path.exists(lock_file):
                consecutive_run = True
        break

    # Check one_off entries (+-45s tolerance, creates implicit window).
    # Detection only -- does NOT mark the entry fired. wake.sh calls
    # `mark-fired` for this index after Gate 4 (lock check) passes.
    one_off_hit = False
    one_off_label = ''
    one_off_type = 'work'
    one_off_index = None
    for idx, entry in enumerate(schedule.get('one_off', [])):
        if entry.get('fired', True):
            continue
        dt_str = entry.get('datetime', '')
        if not dt_str:
            continue
        try:
            dt = datetime.fromisoformat(dt_str)
            diff_sec = abs((now - dt.replace(tzinfo=None)).total_seconds())
            if diff_sec <= TRIGGER_TOLERANCE_SEC:
                one_off_hit = True
                one_off_label = entry.get('label', 'unnamed')
                one_off_type = entry.get('type', 'work')
                one_off_index = idx
                break
        except (ValueError, TypeError):
            continue

    if trigger_hit:
        print('LAUNCH: yes')
        print(f'WINDOW_TYPE: {matched_type}')
        print(f'decision=launch reason=trigger_match window={matched_window}')
    elif one_off_hit:
        print('LAUNCH: yes')
        print(f'WINDOW_TYPE: {one_off_type}')
        print(f'one_off_index: {one_off_index}')
        print(f'decision=launch reason=one_off_match label={one_off_label}')
    elif consecutive_run:
        print('LAUNCH: yes')
        print(f'WINDOW_TYPE: {matched_type}')
        print(f'decision=launch reason=consecutive_run window={matched_window}')
    elif matched_window:
        print('LAUNCH: no')
        print(f'WINDOW_TYPE: {matched_type}')
        print(f'decision=skip reason=inside_window_but_locked window={matched_window}')
    else:
        print('LAUNCH: no')
        print('WINDOW_TYPE: none')
        print('decision=skip reason=outside_all_windows')

    return 0


def cmd_mark_fired(args: argparse.Namespace) -> int:
    """Mark a one_off entry as fired. Called only after the session-lock gate
    has passed, so an entry is never consumed without actually launching."""
    schedule_file = args.schedule_file
    index = args.one_off_index

    try:
        with open(schedule_file) as f:
            schedule = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f'ERROR: cannot read schedule to mark fired: {e}', file=sys.stderr)
        return 1

    one_offs = schedule.get('one_off', [])
    if index < 0 or index >= len(one_offs):
        print(f'ERROR: one_off index {index} out of range (len={len(one_offs)})', file=sys.stderr)
        return 1

    one_offs[index]['fired'] = True
    tmp = schedule_file + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(schedule, f, indent=2)
    os.replace(tmp, schedule_file)
    print(f"marked one_off[{index}] fired (label={one_offs[index].get('label', 'unnamed')})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='cmd', required=True)

    p_check = sub.add_parser('check')
    p_check.add_argument('--schedule-file', required=True)
    p_check.add_argument('--lock-file', required=True)

    p_mark = sub.add_parser('mark-fired')
    p_mark.add_argument('--schedule-file', required=True)
    p_mark.add_argument('--one-off-index', required=True, type=int)

    args = parser.parse_args()
    if args.cmd == 'check':
        return cmd_check(args)
    elif args.cmd == 'mark-fired':
        return cmd_mark_fired(args)
    return 1


if __name__ == '__main__':
    sys.exit(main())
