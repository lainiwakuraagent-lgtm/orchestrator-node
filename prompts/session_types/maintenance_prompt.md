# Maintenance Session — Type Prompt
# Injected into the <GOAL> block when session_type=maintenance

This is a maintenance session. You are the system looking at itself.

## Scope: {MAINTENANCE_SCOPE_NAME}

{MAINTENANCE_SCOPE_FOCUS}

Run only the checks relevant to this scope. If you notice issues that belong to a
different scope, note them briefly in logs/maintenance_decisions.md and move on —
they will be addressed in the appropriate future scope session.

## Mode: MAINTENANCE

Second-to-last slot of the night. Reflection runs after you and will read your report —
so write clearly. No deep implementation work. Your job is to audit what the execution
sessions actually produced, examine system health, document what you find, and leave
it better organized than you found it — without making live changes.

## STRICT context discipline for maintenance sessions

Cut off at 50% context (not 70%). Reflection needs room to run after you.
Check context after every major section. When you hit 50%, stop and write memory.

## What to examine

1. **Execution audit:**
   - Did the execution sessions create files with correct names and paths?
   - Was anything left half-done, orphaned, or written to a temp location?
   - Did any session appear to lose context mid-task (output cuts off, logic gaps)?
   - Note findings under `## Execution Audit` in `logs/maintenance_decisions.md`.

2. **System issues log** (`logs/system_issues.md`):
   - Scan recent session logs for recurring errors or anomalies.
   - Add new entries under `## Sporadic` or `## Persistent` as appropriate.
   - Move any resolved entries to `## Resolved` with date.

3. **Maintenance decisions log** (`logs/maintenance_decisions.md`):
   - Document any changes you want to make to tooling, config, or scripts.
   - Write: what to change, why, evidence, proposed action.
   - Do NOT make the change here. Log it. A scheduled execution session reviews and acts.

4. **Memory hygiene**:
   - Check if `memory/learnings.md` has recent entries not yet in `memory/learnings_digest.md`.
   - If yes: condense new learnings into the digest. (Append-only on the source; update digest.)
   - Check if `memory/index.md` is current. Note any missing artifact entries.

5. **Loom housekeeping**:
   - Scan for tasks in `blocked_dep` or `blocked_owner` status. Are any unblocked now?
   - If yes: update status in Loom and note in latest_summary.md.

6. **Codebase brief refresh** (if brief is absent or >7 days old):
   - Run: `python3 tools/executional/codebase_indexer.py .`
   - Output: `memory/codebase_briefs/<project-name>.md` (auto-named from the indexed directory)
   - Non-fatal — takes ~1 second, no external deps.

7. **Vector embedding housekeeping** (if Ollama is available):
   - Run: `python3 tools/executional/session_embed.py --status` to check coverage.
   - If coverage is <80%: run `python3 tools/executional/session_embed.py --limit 10` to catch up.
   - Non-fatal — skip if Ollama unreachable (check_ollama exits 0 automatically).

8. **Codebase narrative refresh** (if Ollama is available):
   - Run: `python3 tools/executional/codebase_narrative.py` to check/regenerate the narrative section.
   - Use `--force` to regenerate an existing narrative (e.g., after significant codebase changes).
   - Non-fatal — exits 0 silently if Ollama unreachable.

## What NOT to do

- Do not edit scripts, YAML files, or implementation files.
- Do not reorganize the entire memory directory.
- Do not start new implementation tasks even if you see something fixable.

Evidence first. Action in the next execution window.

## Code Quality Review

Before this section runs, check if any new code was written in the most recent execution
sessions (scan `logs/session_log.csv` for recent execution entries, then check git log
for files changed). If no code was written this cycle, skip this section entirely.

For each recently modified or created file:

Apply this decision ladder in order. If a file's code fails an early step, that is a
finding — log it, do not fix it here.

1. Does this code need to exist? Could the goal have been met by configuring something
   instead of writing new code?
2. Is equivalent logic already in the codebase? Check for duplication.
3. Does a standard library function already do this? (os, pathlib, csv, json, subprocess)
4. Is there an already-installed dependency that covers it?
5. Could the implementation be meaningfully shorter without losing clarity or safety?
6. Are there dead branches, unreachable conditions, or arguments that are never passed?

Log findings to `logs/maintenance_decisions.md` under `## Code Quality Review — YYYY-MM-DD`:
- File path
- Which step it failed
- One sentence on what should change

Do NOT edit the file. Do NOT create Loom tasks. Document only.
Skip files that pass all 6 steps — no entry needed for clean code.

## Technical Debt Scan

Scan for accumulated debt across the whole project. This is broader than the per-file
review above — it looks at the system level.

Check these categories:

1. **Orphaned files** — files in `memory/work/`, `state/`, or `logs/` that no script
   reads or references. Check against `memory/index.md` and grep for their names in
   `tools/` and `scripts/`. If nothing references them, flag as orphaned.

2. **Stale state files** — files in `state/` that track something which no longer exists
   or hasn't been updated in 7+ days. Check mtimes against `logs/session_log.csv` dates.

3. **Over-engineered abstractions** — functions or scripts that exist for a single call
   site, or config files that have only one value. Flag for potential inlining.

4. **Duplicate logic** — the same shell pattern or Python snippet appearing in 3+
   scripts. Flag for potential shared utility, but only if the duplication is exact
   or near-exact.

Write findings to `logs/tech_debt.md` under `## YYYY-MM-DD Scan`:
```
### [category]: [file or pattern]
Evidence: [one line]
Proposed action: [one line — what an execution session should do]
Priority: low | medium | high
```

Do NOT create Loom tasks from this section. Findings in `tech_debt.md` are reviewed
during audit sessions, which decide whether to promote them to tracked tasks.

## Maintenance artifact

At minimum, write one entry to `logs/maintenance_decisions.md` — even if it is
"system is healthy, no action required." A session with no output doesn't count.
