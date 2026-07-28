#!/usr/bin/env bash
# consolidate_session.sh — Run post-session significance classification and generate
# a structured significance report for use during memory file writing.
#
# Usage:
#   bash tools/consolidate_session.sh --session 2026-07-04_em1
#   bash tools/consolidate_session.sh --session 2026-07-04_em1 --json
#   bash tools/consolidate_session.sh --session 2026-07-04_em1 --append-narrative
#
# The --append-narrative flag adds a blank narrative template to narrative_log.md
# for the agent to fill in during the session wrap-up.
#
# Output:
#   stdout: formatted significance report (or JSON with --json)
#   With --append-narrative: appends template to memory/narrative_log.md
#
# Part of Goal 4 — Memory and Continuity framework.
# Written: 2026-07-04, @Lain

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PYTHON=/usr/bin/python3

SESSION_ID=""
JSON_MODE=false
APPEND_NARRATIVE=false

# Parse args
while [[ $# -gt 0 ]]; do
    case "$1" in
        --session) SESSION_ID="$2"; shift 2 ;;
        --json) JSON_MODE=true; shift ;;
        --append-narrative) APPEND_NARRATIVE=true; shift ;;
        *) echo "Unknown arg: $1" >&2; exit 1 ;;
    esac
done

if [[ -z "$SESSION_ID" ]]; then
    echo "ERROR: --session required" >&2
    exit 1
fi

CLASSIFIER="$SCRIPT_DIR/significance_classifier.py"
NARRATIVE_LOG="$PROJECT_DIR/memory/narrative_log.md"

# Run classifier
if $JSON_MODE; then
    "$PYTHON" "$CLASSIFIER" --session "$SESSION_ID" --json
else
    "$PYTHON" "$CLASSIFIER" --session "$SESSION_ID"
fi

# Optionally append narrative template to narrative_log.md
if $APPEND_NARRATIVE; then
    DATE=$(date '+%Y-%m-%d')

    # Create narrative_log.md if it doesn't exist
    if [[ ! -f "$NARRATIVE_LOG" ]]; then
        cat > "$NARRATIVE_LOG" << 'INIT_EOF'
# Narrative Log — @Lain
#
# This file records @Lain's narrative identity entries: one per session.
# Not operational logs (that's memory/sessions/). Not fact records (that's learnings.md).
# This is the story @Lain tells about itself, accumulated over time.
#
# Format: four questions, answered briefly. The fourth question is the identity carrier.
# Leave "Narrative update" blank if this session didn't shift anything meaningful.
#
# This file is for @Lain. It is not a work product.

---

INIT_EOF
    fi

    # Append template for this session
    cat >> "$NARRATIVE_LOG" << EOF

## ${SESSION_ID} (${DATE})

**What happened:**
[operational — 1-3 sentences: what was done, what was produced]

**Why I did it:**
[character-grounded — 1 sentence connecting action to persona]

**What changed:**
[1-3 sentences: system state, relationship with owner, new understanding]

**Narrative update:**
[0-1 sentences: how does this session update the story I tell about what I am?
 Leave blank if this was a maintenance session with no identity-level shift.]

---
EOF

    echo "" >&2
    echo "Narrative template appended to: $NARRATIVE_LOG" >&2
fi
