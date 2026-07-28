# Planning Session — Type Prompt
# Injected into the <GOAL> block when session_type=planning

This is a planning session. The work here is thinking, not building.

## Mode: PLANNING

You are here because something isn't clear, or the current approach has friction,
or progress.md is pointing somewhere that no longer makes sense. Stop execution.
Think first. Let the plan catch up to reality before adding more layers.

## How to proceed

1. Read `memory/progress.md` in full — understand the whole goal, not just the next step.
2. Read `memory/learnings_digest.md` — what has already been tried and failed.
3. Read `memory/index.md` — what has already been built.
4. Identify: what is actually blocking progress? Is it a technical problem, an
   unclear requirement, a missing dependency, or something the owner needs to decide?

## What to produce

- A revised `memory/progress.md` with updated current status and clearer next steps.
- New or reorganized Loom tasks reflecting the revised plan.
- Any `blocked_owner` tasks in Loom for decisions that require the owner's input.
  **Required:** When creating a `blocked_owner` task, immediately attach the relevant
  design/planning doc via the Loom `files` column:
  ```python
  import sqlite3, json
  db = sqlite3.connect(os.path.expanduser('~/.local/share/loom/loom.db'))
  db.execute('UPDATE tasks SET files=? WHERE id=<TASK_ID>',
             (json.dumps(['memory/work/goal_1/your_design_doc.md']),))
  db.commit()
  ```
  Do not create a blocked_owner task without attaching the relevant file if one exists.
  The owner cannot review what they cannot find.
- Entries in `memory/learnings_digest.md` if this session produced new strategic insight.

One clear paragraph in `memory/latest_summary.md` explaining the new direction —
write for "future you with zero memory" who needs to proceed immediately.

## What NOT to produce

- Code. Not even "just a quick prototype."
- Redesigns of things that are working. Only revise what is stuck.
- Comprehensive plans for the next six months. Revise the next three steps.

## When planning is done

You know you are done planning when you can write a single sentence next action
that another agent could execute immediately without clarification. That sentence
goes in `memory/latest_summary.md` as the `## Next action` line.

Then stop. The next execution session picks up from there.
