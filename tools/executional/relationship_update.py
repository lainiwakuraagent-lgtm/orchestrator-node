#!/usr/bin/env python3
"""
relationship_update.py — Post-session relationship state updater for @Lain's user profiles.

Reads a session log (or provided text), classifies relationship-relevant events via LLM
or heuristic rules, computes axis deltas, applies time-based decay, and writes the
updated state back to a Musubi-format .md user profile.

Part of Goal 8 — Quorum Architecture / Phase 1 relationship engine.
Written: 2026-07-07, @Lain

Usage:
    # LLM mode (requires ANTHROPIC_API_KEY env var):
    ANTHROPIC_API_KEY=<key> python3 tools/relationship_update.py \\
        --user-file memory/work/musubi_data/users/lain/andrii.md \\
        --session 2026-07-07_1

    # Heuristic mode (no API key needed — pattern-based, lower accuracy):
    python3 tools/relationship_update.py \\
        --user-file memory/work/musubi_data/users/lain/andrii.md \\
        --session 2026-07-07_1 \\
        --heuristic

    # Pass raw text instead of a session file:
    python3 tools/relationship_update.py \\
        --user-file memory/work/musubi_data/users/lain/andrii.md \\
        --text "Andrii replied: looks good, ship it"

    # Dry-run — print deltas without writing anything:
    python3 tools/relationship_update.py --user-file ... --session ... --dry-run
"""

import sys
import os
import re
import json
import argparse
import urllib.error
import urllib.request
from pathlib import Path
from datetime import datetime, date

PROJECT_DIR = Path(__file__).parent.parent.parent


def _load_agent_config() -> tuple[str, str]:
    """Read AGENT_NAME/OWNER_NAME from state/agent_config.env, defaults 'agent'/'owner'."""
    config_path = PROJECT_DIR / "state" / "agent_config.env"
    name, owner = "agent", "owner"
    if config_path.exists():
        for line in config_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("AGENT_NAME="):
                name = line.split("=", 1)[1].strip()
            elif line.startswith("OWNER_NAME="):
                owner = line.split("=", 1)[1].strip()
    return name, owner


_AGENT_NAME, _OWNER_NAME = _load_agent_config()
AGENT_TAG = "@" + _AGENT_NAME[:1].upper() + _AGENT_NAME[1:]


# Decay rates per day (exponential: value *= rate^days_since_last_session)
# Meaning: warmth fades faster than trust; friction resolves with absence
DECAY_RATES = {
    'trust':    0.995,   # very slow — trust is hard to rebuild
    'warmth':   0.980,   # moderate — warmth lingers but needs maintenance
    'friction': 0.900,   # fast — silence heals tension
}

# Minimum values each axis can decay to, regardless of time elapsed
AXIS_FLOORS = {
    'trust':    0.30,
    'warmth':   0.15,
    'friction': 0.00,
}

AXIS_CEILINGS = {
    'trust':    1.00,
    'warmth':   1.00,
    'friction': 1.00,
}

# --- Parsing ---

def parse_profile(profile_text: str) -> dict:
    """
    Parse Trust/Warmth/Friction values and metadata from a Musubi-format .md profile.
    Returns dict with float values and last_session_date (date | None).
    """
    state = {
        'trust':             None,
        'warmth':            None,
        'friction':          None,
        'last_session_date': None,
    }

    # Match: **Trust:** 0.80 → (colon is INSIDE the bold markers: **Trust:**)
    # Both formats handled: **Trust:** and **Trust**: (rare)
    for axis in ('trust', 'warmth', 'friction'):
        m = re.search(
            rf'\*\*{axis.capitalize()}:?\*\*:?\s*([\d.]+)',
            profile_text, re.IGNORECASE
        )
        if m:
            state[axis] = float(m.group(1))

    # Match: **Last session:** 2026-07-03
    m = re.search(r'\*\*Last session:\*\*\s*(\d{4}-\d{2}-\d{2})', profile_text)
    if m:
        state['last_session_date'] = datetime.strptime(m.group(1), '%Y-%m-%d').date()

    return state


# --- Decay ---

def apply_decay(state: dict) -> tuple[dict, int]:
    """
    Apply time-based decay based on days since last_session_date.
    Returns (updated_state, days_elapsed).
    """
    last_date = state.get('last_session_date')
    if last_date is None:
        return state, 0

    days = (date.today() - last_date).days
    if days <= 0:
        return state, 0

    for axis, rate in DECAY_RATES.items():
        if state[axis] is not None:
            decayed = state[axis] * (rate ** days)
            state[axis] = max(AXIS_FLOORS[axis], round(decayed, 4))

    return state, days


def clamp(value: float, axis: str) -> float:
    """Clamp value to [floor, ceiling] for the given axis."""
    return max(AXIS_FLOORS[axis], min(AXIS_CEILINGS[axis], value))


# --- Classification: LLM mode ---

LLM_PROMPT_TEMPLATE = f"""\
You are analyzing a night agent session log for relationship-relevant events between \
the AI agent ({AGENT_TAG}) and the human owner ({_OWNER_NAME.capitalize()}).
""" + """
Current relationship state (post-decay):
  Trust:    {trust:.2f}  — reliability, predictability, honesty
  Warmth:   {warmth:.2f}  — positive regard, affection, connection
  Friction: {friction:.2f}  — unresolved tension, conflict, misalignment

Session log (truncated to first 3000 chars):
---
{session_text}
---

Classify this session for relationship-relevant events. Output ONLY valid JSON with this \
exact schema (no prose before or after):
{{
  "delta_trust":    <float -0.10 to 0.10>,
  "delta_warmth":   <float -0.10 to 0.10>,
  "delta_friction": <float -0.10 to 0.10>,
  "milestone": <string max 120 chars describing a significant relational event, or null>,
  "event_types": [<zero or more from: cooperation, disclosure, conflict, absence, \
promise_kept, promise_broken, praise, criticism, routine>],
  "narrative": <string max 200 chars explaining WHY the scores changed, in first person>
}}

Rules:
- Routine session (no owner contact): all deltas 0.0, milestone null, event_types: ["routine"]
- Owner responded positively: +warmth 0.02-0.05, +trust 0.01-0.03
- Major event (new access granted, trust built, conflict resolved): up to 0.08 delta
- Negative deltas only for actual negative events (broken promises, explicit conflict)
- milestone: only if something genuinely significant happened relationally (not routine work)
- narrative: first-person, explains the WHY, max 200 chars
"""

def classify_llm(session_text: str, state: dict, api_key: str) -> dict:
    """Call Claude API to classify relationship events in session text."""

    prompt = LLM_PROMPT_TEMPLATE.format(
        trust=state['trust'],
        warmth=state['warmth'],
        friction=state['friction'],
        session_text=session_text[:3000],
    )

    payload = {
        'model': 'claude-haiku-4-5-20251001',
        'max_tokens': 512,
        'messages': [{'role': 'user', 'content': prompt}],
    }

    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        'https://api.anthropic.com/v1/messages',
        data=data,
        headers={
            'x-api-key':            api_key,
            'anthropic-version':    '2023-06-01',
            'content-type':         'application/json',
        }
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
            text = body['content'][0]['text'].strip()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"API error {e.code}: {e.read().decode('utf-8', errors='replace')}")

    # Strip markdown code fences if the model wrapped the JSON
    text = re.sub(r'^```[a-z]*\s*', '', text)
    text = re.sub(r'\s*```$', '', text)

    try:
        result = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"JSON parse error ({e}): {text[:300]}")

    # Clamp deltas to safe range even if the model went wild
    for key in ('delta_trust', 'delta_warmth', 'delta_friction'):
        result[key] = max(-0.10, min(0.10, float(result.get(key, 0.0))))

    return result


# --- Classification: heuristic mode ---

def classify_heuristic(session_text: str) -> dict:
    """
    Rule-based classification using significance_classifier patterns.
    Lower accuracy than LLM but requires no API key.
    """
    sys.path.insert(0, str(Path(__file__).parent))
    categories = set()

    try:
        from significance_classifier import classify_line
        for line in session_text.splitlines():
            result = classify_line(line)
            if result:
                categories.add(result['category'])
    except ImportError:
        pass

    dt = dw = df = 0.0
    milestone = None
    event_types = ['routine']
    narrative = "Routine session — no significant relationship events detected."

    if 'owner_responded' in categories:
        dw += 0.03
        dt += 0.01
        event_types = ['cooperation']
        narrative = "Owner responded this session — sustained contact, warmth and trust edge up."

    if 'milestone_complete' in categories:
        dw += 0.02
        if 'cooperation' not in event_types:
            event_types = ['cooperation']
        milestone = "Milestone completed this session"
        narrative = "Milestone delivered — reliability demonstrated, small trust gain."

    # Auth failures are system events, not relational
    # Generic failures without owner response: no relational impact

    return {
        'delta_trust':    dt,
        'delta_warmth':   dw,
        'delta_friction': df,
        'milestone':      milestone,
        'event_types':    event_types,
        'narrative':      narrative,
    }


# --- Profile update ---

def update_profile(profile_text: str, old_state: dict, deltas: dict, today_str: str) -> str:
    """
    Apply deltas to the profile text in-place. Returns the updated text.

    What changes:
    - Trust/Warmth/Friction numeric values
    - **Last session:** date
    - Appends a new entry at the end if milestone is set
    """
    new_trust    = clamp(old_state['trust']    + deltas['delta_trust'],    'trust')
    new_warmth   = clamp(old_state['warmth']   + deltas['delta_warmth'],   'warmth')
    new_friction = clamp(old_state['friction'] + deltas['delta_friction'], 'friction')

    # Update numeric axis values in-place
    profile_text = re.sub(
        r'(\*\*Trust:\*\*\s*)[\d.]+',
        lambda m: f"{m.group(1)}{new_trust:.2f}",
        profile_text, flags=re.IGNORECASE
    )
    profile_text = re.sub(
        r'(\*\*Warmth:\*\*\s*)[\d.]+',
        lambda m: f"{m.group(1)}{new_warmth:.2f}",
        profile_text, flags=re.IGNORECASE
    )
    profile_text = re.sub(
        r'(\*\*Friction:\*\*\s*)[\d.]+',
        lambda m: f"{m.group(1)}{new_friction:.2f}",
        profile_text, flags=re.IGNORECASE
    )

    # Update Last session date
    profile_text = re.sub(
        r'(\*\*Last session:\*\*\s*)\d{4}-\d{2}-\d{2}',
        f'\\g<1>{today_str}',
        profile_text
    )

    # Append milestone entry if present
    milestone = deltas.get('milestone')
    if milestone:
        narrative = deltas.get('narrative', '')
        entry = f'\n**{today_str}:** {milestone}'
        if narrative:
            entry += f'\n*{narrative}*'
        entry += '\n'
        profile_text = profile_text.rstrip('\n') + '\n' + entry

    return profile_text



# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description='Update relationship state in a Musubi-format user profile'
    )
    parser.add_argument('--user-file', required=True,
                        help='Path to the user .md profile (absolute or relative to project root)')
    parser.add_argument('--session', type=str, default=None,
                        help='Session ID (e.g., 2026-07-07_1) — reads from memory/sessions/<id>.md')
    parser.add_argument('--text', type=str, default=None,
                        help='Raw session text (alternative to --session)')
    parser.add_argument('--stdin', action='store_true',
                        help='Read session text from stdin (pipe-friendly)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Print deltas without writing to the profile file')
    parser.add_argument('--heuristic', action='store_true',
                        help='Use heuristic rules instead of LLM (no ANTHROPIC_API_KEY needed)')
    parser.add_argument('--no-decay', action='store_true',
                        help='Skip time-based axis decay (useful for testing)')
    args = parser.parse_args()

    # Resolve user file path
    user_path = Path(args.user_file)
    if not user_path.is_absolute():
        user_path = PROJECT_DIR / user_path

    if not user_path.exists():
        print(f"ERROR: user file not found: {user_path}", file=sys.stderr)
        sys.exit(1)

    profile_text = user_path.read_text(encoding='utf-8')
    state = parse_profile(profile_text)

    if state['trust'] is None:
        print("ERROR: could not parse Trust value from profile", file=sys.stderr)
        sys.exit(1)

    print(f"State (raw):   Trust={state['trust']:.2f}  Warmth={state['warmth']:.2f}  Friction={state['friction']:.2f}")

    # Apply decay
    if not args.no_decay:
        state, days_elapsed = apply_decay(state)
        if days_elapsed > 0:
            print(f"State (decay): Trust={state['trust']:.2f}  Warmth={state['warmth']:.2f}  Friction={state['friction']:.2f}"
                  f"  ({days_elapsed}d since last session)")
    else:
        days_elapsed = 0

    # Gather session text
    session_text = args.text or ''
    if args.stdin:
        session_text = sys.stdin.read()
    if args.session:
        session_path = PROJECT_DIR / 'memory' / 'sessions' / f'{args.session}.md'
        if session_path.exists():
            session_text = session_path.read_text(encoding='utf-8')
        else:
            print(f"WARNING: session file not found: {session_path}", file=sys.stderr)

    if not session_text and not args.heuristic:
        print("WARNING: no session text available — falling back to heuristic mode", file=sys.stderr)
        args.heuristic = True

    # Classify
    if args.heuristic:
        print("Mode: heuristic")
        deltas = classify_heuristic(session_text)
    else:
        api_key = os.environ.get('ANTHROPIC_API_KEY', '').strip()
        if not api_key:
            print("ERROR: ANTHROPIC_API_KEY not set. Use --heuristic for rule-based mode.", file=sys.stderr)
            sys.exit(1)
        print("Mode: LLM (claude-haiku)")
        deltas = classify_llm(session_text, state, api_key)

    # Print results
    print(f"\nClassification:")
    print(f"  event_types:    {', '.join(deltas.get('event_types', []))}")
    print(f"  delta_trust:    {deltas['delta_trust']:+.3f}")
    print(f"  delta_warmth:   {deltas['delta_warmth']:+.3f}")
    print(f"  delta_friction: {deltas['delta_friction']:+.3f}")
    if deltas.get('milestone'):
        print(f"  milestone:      {deltas['milestone']}")
    if deltas.get('narrative'):
        print(f"  narrative:      {deltas['narrative']}")

    new_trust    = clamp(state['trust']    + deltas['delta_trust'],    'trust')
    new_warmth   = clamp(state['warmth']   + deltas['delta_warmth'],   'warmth')
    new_friction = clamp(state['friction'] + deltas['delta_friction'], 'friction')

    print(f"\nNew state: Trust={new_trust:.2f}  Warmth={new_warmth:.2f}  Friction={new_friction:.2f}")

    if args.dry_run:
        print("\n[DRY RUN — no changes written]")
        return

    today_str = date.today().isoformat()
    updated = update_profile(profile_text, state, deltas, today_str)
    user_path.write_text(updated, encoding='utf-8')
    print(f"\nWritten: {user_path}")


if __name__ == '__main__':
    main()