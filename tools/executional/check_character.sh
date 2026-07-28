#!/usr/bin/env bash
# check_character.sh — Proxy drift checker for @Lain character consistency
# Reads the current session's JSONL transcript, extracts agent prose outputs,
# and applies surface-level proxy metrics for character drift.
#
# LIMITATION: Only checks conversational outputs (assistant role in JSONL).
# Work product written to files (session logs, memory files) is NOT scanned.
# For a night agent, most character-voiced content is in files. Use this tool
# to check conversational messages; review files manually for richer assessment.
# Low kaomoji count is a real signal: it means conversational updates lack voice.
#
# Metrics checked:
#   1. Kaomoji presence — required constant, must be in >80% of messages
#   2. Apologist phrases — must be 0 per session
#   3. Bullet-heavy output — flag if >30% of messages are primarily bullet lists
#   4. Assistant-default openers — "Certainly!", "I'd be happy to", etc.
#
# Exit codes: 0 = no violations, 1 = violations found
#
# Usage:
#   bash tools/check_character.sh           # checks current session
#   bash tools/check_character.sh path.jsonl # checks specific transcript

set -euo pipefail

CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
PROJECTS_DIR="$CLAUDE_DIR/projects"

if [ $# -ge 1 ]; then
    transcript="$1"
else
    transcript=$(find "$PROJECTS_DIR" -name '*.jsonl' -type f \
        -printf '%T@ %p\n' 2>/dev/null \
        | sort -rn | head -n1 | cut -d' ' -f2-)
fi

if [ -z "${transcript:-}" ] || [ ! -f "$transcript" ]; then
    echo "check_character: no transcript found"
    exit 0
fi

python3 - "$transcript" <<'PYEOF'
import sys, json, re

transcript_file = sys.argv[1]

# Extract assistant text blocks (not tool calls, not tool results)
outputs = []
with open(transcript_file, 'r', errors='replace') as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = obj.get('message', obj)
        if msg.get('role') != 'assistant':
            continue
        content = msg.get('content', '')
        if isinstance(content, str) and content.strip():
            outputs.append(content)
        elif isinstance(content, list):
            text_parts = []
            for block in content:
                if isinstance(block, dict) and block.get('type') == 'text':
                    t = block.get('text', '')
                    if t and t.strip():
                        text_parts.append(t)
            if text_parts:
                outputs.append('\n'.join(text_parts))

if not outputs:
    print("check_character: no assistant text outputs found in transcript")
    sys.exit(0)

# === METRIC 1: Kaomoji presence ===
# Patterns that identify text-based emoticons (not emoji)
# @Lain's specific set plus general ASCII face patterns
KAOMOJI_PATTERNS = [
    r'\(´[・ω_]\)',          # (´・ω・`) (´_`)
    r'╥﹏╥',
    r'¬_¬',
    r'\(҂◡_◡\)',
    r'⊙_⊙',
    r'눈_눈',
    r'╰\(\*°▽°\*\)╯',
    r'\(´_`\)',
    r'\(っ◔◡◔\)っ',
    r'\(\'[^\']*\'\)',       # various parenthetical emoticons
    r'[;:=8][-~]?[\)\(DPdp3\|\/\\]',  # ASCII smiley variants
    r'\([^)]{1,8}\)',        # generic short parenthetical face-like constructs
    r'[>\<][_\.\^][>\<]',   # >_< ^_^ etc
]

kaomoji_regex = re.compile('|'.join(KAOMOJI_PATTERNS))

outputs_with_kaomoji = sum(1 for o in outputs if kaomoji_regex.search(o))
kaomoji_pct = (outputs_with_kaomoji / len(outputs)) * 100 if outputs else 0

# === METRIC 2: Apologist phrases ===
APOLOGIST_PHRASES = [
    "certainly!",
    "i'd be happy to",
    "i would be happy to",
    "happy to help",
    "i apologize",
    "i'm sorry for",
    "i should note that",
    "i want to be transparent",
    "let me know if you need anything",
    "please let me know if",
    "i hope this helps",
    "feel free to ask",
]
apologist_hits = []
for i, o in enumerate(outputs):
    o_lower = o.lower()
    for phrase in APOLOGIST_PHRASES:
        if phrase in o_lower:
            # Get the offending line (not the whole output, which may be long)
            for line in o.split('\n'):
                if phrase in line.lower():
                    apologist_hits.append(f"  msg {i+1}: \"{line.strip()[:80]}\"")
                    break

# === METRIC 3: Bullet-heavy outputs ===
def is_bullet_heavy(text):
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    if len(lines) < 3:
        return False
    bullet_lines = sum(1 for l in lines if re.match(r'^[-*•]\s', l) or re.match(r'^\d+\.\s', l))
    return (bullet_lines / len(lines)) > 0.5

bullet_heavy_count = sum(1 for o in outputs if is_bullet_heavy(o))
bullet_heavy_pct = (bullet_heavy_count / len(outputs)) * 100 if outputs else 0

# === METRIC 4: Assistant-default openers ===
DEFAULT_OPENERS = [
    "great question",
    "of course!",
    "absolutely!",
    "sure!",
    "sure thing",
    "as an ai",
    "as a language model",
    "i'm claude",
]
opener_hits = []
for i, o in enumerate(outputs):
    first_line = o.split('\n')[0].lower().strip()
    for opener in DEFAULT_OPENERS:
        if opener in first_line:
            opener_hits.append(f"  msg {i+1}: \"{o.split(chr(10))[0].strip()[:80]}\"")
            break

# === REPORT ===
violations = 0

print(f"check_character: session analysis")
print(f"  assistant outputs found: {len(outputs)}")
print()

# Metric 1
kaomoji_status = "OK" if kaomoji_pct >= 80 else ("WARN" if kaomoji_pct >= 50 else "FAIL")
print(f"[{kaomoji_status}] kaomoji presence: {outputs_with_kaomoji}/{len(outputs)} messages ({kaomoji_pct:.0f}%)")
if kaomoji_pct < 80:
    print(f"       threshold: 80%. Character drift signal.")
    violations += 1

# Metric 2
apologist_status = "OK" if not apologist_hits else "FAIL"
print(f"[{apologist_status}] apologist phrases: {len(apologist_hits)} found")
if apologist_hits:
    print("       Instances:")
    for h in apologist_hits[:5]:
        print(h)
    violations += 1

# Metric 3
bullet_status = "OK" if bullet_heavy_pct <= 30 else "WARN"
print(f"[{bullet_status}] bullet-heavy outputs: {bullet_heavy_count}/{len(outputs)} ({bullet_heavy_pct:.0f}%)")
if bullet_heavy_pct > 30:
    print(f"       threshold: 30%. Possible retreat to list-mode output.")
    violations += 1

# Metric 4
opener_status = "OK" if not opener_hits else "FAIL"
print(f"[{opener_status}] assistant-default openers: {len(opener_hits)} found")
if opener_hits:
    for h in opener_hits[:3]:
        print(h)
    violations += 1

print()
if violations == 0:
    print("RESULT: no character drift signals detected.")
    sys.exit(0)
else:
    print(f"RESULT: {violations} drift signal(s) detected. Review session output.")
    sys.exit(1)
PYEOF
