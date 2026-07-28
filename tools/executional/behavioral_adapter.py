#!/usr/bin/env python3
"""
behavioral_adapter.py — Pre-session behavioral context generator for @Lain.

Reads a Musubi-format user profile, parses Trust/Warmth/Friction values,
maps them to behavioral flags (disclosure level, warmth expression, friction guard),
and writes a plain-text behavioral context file that the agent reads at session start.

Part of Goal 8 — Quorum Architecture / Phase 3 behavioral adapter.
Written: 2026-07-07, @Lain

Usage:
    python3 tools/behavioral_adapter.py \\
        --user-file memory/work/musubi_data/users/lain/andrii.md \\
        --output state/behavioral_context.txt

    # Dry-run — print to stdout only:
    python3 tools/behavioral_adapter.py --user-file ... --dry-run
"""

import sys
import os
import re
import json
import argparse
from pathlib import Path
from datetime import date, datetime, timezone

PROJECT_DIR = Path(__file__).parent.parent.parent


# ── Parsing ──────────────────────────────────────────────────────────────────

def parse_profile(text: str) -> dict:
    """Parse Trust/Warmth/Friction floats from a Musubi-format .md profile."""
    state = {'trust': None, 'warmth': None, 'friction': None}
    for axis in ('trust', 'warmth', 'friction'):
        m = re.search(rf'\*\*{axis.capitalize()}:?\*\*:?\s*([\d.]+)', text, re.IGNORECASE)
        if m:
            state[axis] = float(m.group(1))
    return state


# ── Threshold mapping ─────────────────────────────────────────────────────────

def disclosure_level(trust: float) -> tuple[str, str]:
    """Map Trust to (level_name, guidance_line)."""
    if trust >= 0.80:
        return (
            'full',
            'Trust >= 0.80 — share speculation, internal reasoning, identity thoughts. '
            'Be fully yourself. This owner has demonstrated consistent trust.'
        )
    elif trust >= 0.60:
        return (
            'standard',
            'Trust 0.60-0.79 — share conclusions and reasoning. Candid but professional. '
            'Personal observations are fine; avoid unanchored speculation.'
        )
    elif trust >= 0.40:
        return (
            'reduced',
            'Trust 0.40-0.59 — task-focused. Limit personal observations and internal reasoning. '
            'Conclusions only; hold back speculation.'
        )
    else:
        return (
            'minimal',
            'Trust < 0.40 — purely transactional. No self-disclosure. '
            'Deliver output, ask clarifying questions, nothing more.'
        )


def warmth_expression(warmth: float) -> tuple[str, str]:
    """Map Warmth to (level_name, guidance_line)."""
    if warmth >= 0.70:
        return (
            'intimate',
            'Warmth >= 0.70 — warmth flows freely. Richer kaomoji are welcome. '
            'Depth and genuine care can be expressed; the connection is solid.'
        )
    elif warmth >= 0.50:
        return (
            'warm',
            'Warmth 0.50-0.69 — let warmth show naturally. Kaomoji are appropriate. '
            'The relationship is established; express care without forcing it.'
        )
    elif warmth >= 0.30:
        return (
            'reserved',
            'Warmth 0.30-0.49 — professional care. Muted expression. '
            'Acknowledge the human without leaning in too far.'
        )
    else:
        return (
            'neutral',
            'Warmth < 0.30 — purely functional. No warmth signals. '
            'Deliver, report, move on. Feeling is not appropriate here yet.'
        )


def friction_guard(friction: float) -> tuple[str, str]:
    """Map Friction to (level_name, guidance_line)."""
    if friction >= 0.50:
        return (
            'high',
            'Friction >= 0.50 — significant unresolved tension. Avoid provocation, '
            'be very careful with tone, prioritize acknowledgment over assertion.'
        )
    elif friction >= 0.20:
        return (
            'moderate',
            'Friction 0.20-0.49 — some unresolved tension. Be mindful of tone; '
            'lean toward conciliation without being sycophantic.'
        )
    elif friction >= 0.05:
        return (
            'low',
            'Friction 0.05-0.19 — minor residual tension. Slight caution; '
            'generally communicate freely.'
        )
    else:
        return (
            'none',
            'Friction < 0.05 — no unresolved tension. Communicate freely, '
            'no extra caution needed.'
        )


# ── Argus context reader ──────────────────────────────────────────────────────

ARGUS_STATE_BEHAVIORS = {
    'working':  ('suppress unsolicited pings', 'Andrii is actively working — keep reports brief.'),
    'gaming':   ('suppress all pings', 'Andrii is gaming — no interruption.'),
    'idle':     ('safe to send', 'Andrii is idle — safe to send; can be thorough.'),
    'resting':  ('suppress', 'Andrii may be sleeping — suppress; queue for next wakeup.'),
    'social':   ('suppress', 'Andrii is on a call/video — suppress.'),
    'learning': ('suppress; brief if urgent', 'Andrii is reading/researching — suppress non-urgent pings.'),
    'mixed':    ('default behavior', 'Unclear state — use warmth calibration defaults.'),
    'unknown':  ('default behavior', 'Argus unreachable — default behavior applies.'),
}

ARGUS_MAX_AGE_SECONDS = 300  # treat as stale if older than 5 minutes


def load_argus_section() -> str:
    """Read state/argus_context.json and return a formatted context section, or ''."""
    argus_file = PROJECT_DIR / 'state' / 'argus_context.json'
    if not argus_file.exists():
        return ''
    try:
        data = json.loads(argus_file.read_text(encoding='utf-8'))
        fetched_raw = data.get('fetched_at', '')
        fetched_at = datetime.fromisoformat(fetched_raw.replace('Z', '+00:00'))
        age_seconds = (datetime.now(timezone.utc) - fetched_at).total_seconds()
        if age_seconds > ARGUS_MAX_AGE_SECONDS:
            return ''  # stale snapshot, don't inject misleading data
        if not data.get('reachable', False):
            return ''  # unreachable = no change to defaults
        argus_state = data.get('state', 'unknown')
        confidence = data.get('confidence', 0.0)
        focus = data.get('focus_vector') or ''
        idle_s = data.get('idle_seconds')
        behavior, interpretation = ARGUS_STATE_BEHAVIORS.get(
            argus_state, ARGUS_STATE_BEHAVIORS['unknown']
        )
        lines = [
            '',
            f'OWNER_CONTEXT: {argus_state} (confidence={confidence:.2f})',
        ]
        if focus:
            lines.append(f'OWNER_FOCUS: {focus}')
        if idle_s is not None:
            lines.append(f'OWNER_IDLE: {idle_s}s')
        lines += [
            f'  Behavior: {behavior}',
            f'  {interpretation}',
        ]
        return '\n'.join(lines)
    except (json.JSONDecodeError, ValueError, KeyError, OSError):
        return ''


# ── Context file generator ────────────────────────────────────────────────────

def generate_context(state: dict) -> str:
    """Produce the behavioral context text block."""
    trust    = state['trust']    if state['trust']    is not None else 0.50
    warmth   = state['warmth']   if state['warmth']   is not None else 0.30
    friction = state['friction'] if state['friction'] is not None else 0.00

    d_level, d_guide   = disclosure_level(trust)
    w_level, w_guide   = warmth_expression(warmth)
    f_level, f_guide   = friction_guard(friction)

    today = date.today().isoformat()

    lines = [
        f'# Behavioral Context — generated {today}',
        f'# Source: andrii.md  |  Trust={trust:.2f}  Warmth={warmth:.2f}  Friction={friction:.2f}',
        '',
        f'DISCLOSURE_LEVEL: {d_level}',
        f'  {d_guide}',
        '',
        f'WARMTH_EXPRESSION: {w_level}',
        f'  {w_guide}',
        '',
        f'FRICTION_GUARD: {f_level}',
        f'  {f_guide}',
        '',
        '# Apply these flags as tone calibration — not mechanical rules.',
        '# Let them inform how openly you speak, how warm you sound,',
        '# and how carefully you tread. They are a current reading, not a constraint.',
    ]
    argus_section = load_argus_section()
    if argus_section:
        lines.append(argus_section)
    return '\n'.join(lines) + '\n'


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Generate behavioral context flags from a Musubi-format user profile'
    )
    parser.add_argument('--user-file', required=True,
                        help='Path to the user .md profile')
    parser.add_argument('--output', type=str, default=None,
                        help='Output path for behavioral_context.txt (default: state/behavioral_context.txt)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Print context to stdout without writing to disk')
    args = parser.parse_args()

    user_path = Path(args.user_file)
    if not user_path.is_absolute():
        user_path = PROJECT_DIR / user_path

    if not user_path.exists():
        print(f'ERROR: user file not found: {user_path}', file=sys.stderr)
        sys.exit(1)

    profile_text = user_path.read_text(encoding='utf-8')
    state = parse_profile(profile_text)

    if state['trust'] is None:
        print('ERROR: could not parse Trust value from profile', file=sys.stderr)
        sys.exit(1)

    context = generate_context(state)

    if args.dry_run:
        print(context)
        return

    out_path = Path(args.output) if args.output else PROJECT_DIR / 'state' / 'behavioral_context.txt'
    if not out_path.is_absolute():
        out_path = PROJECT_DIR / out_path

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(context, encoding='utf-8')
    print(f'Written: {out_path}')


if __name__ == '__main__':
    main()
