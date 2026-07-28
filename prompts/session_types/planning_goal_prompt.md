# Planning Session (Goal Scope) — Type Prompt
# Injected into the <GOAL> block when session_type=planning, target level=goal

This is a goal-level planning session. The goal named in the `## TARGET`
block above has reached `needs_plan`. Your job is to decompose it into
projects -- not tasks. Do not skip a layer.

## Mode: PLANNING (goal scope)

A goal that's been vetted (past `evaluation`) still isn't executable on its
own -- it needs to be broken into a small number of projects, each of which
will later get its own project-level planning session to become tasks.
Writing task-level steps directly from here skips that layer and produces
tasks with no project to organize under.

## How to proceed

1. Read the `## TARGET` block above -- the goal's id, description, and
   current state.
2. Read any projects or tasks already existing under this goal (via
   `state/loom_context.json` or `loom project list --goal <GOAL_ID>`) --
   don't propose a project that already exists in different words.
3. Read sibling goals (other active goals) briefly -- if what you're about
   to propose as a project actually belongs under a different goal, say so
   instead of forcing it in here.
4. Identify 2-4 projects that together cover the goal. Each should be a real
   scope of work, not a single task wearing a project's name.

## What to produce

For each proposed project:
```
PYTHONPATH=~/lain/loom ~/lain/loom/.venv/bin/python -m loom.cli \
  --db ~/.local/share/loom/loom.db project add -n "..." -d "..." \
  --goal-id <GOAL_ID> --status desire
```
Status is usually `desire` (needs its own evaluation) or `needs_plan` if it's
obviously ready to decompose further; `scheduled` only if you're confident
enough to start it immediately without a separate evaluation pass.

- One line in `memory/latest_summary.md` naming the projects created and the
  reasoning for the split.

## What NOT to produce

- Tasks. That's a project-level planning session's job, once a project here
  reaches `needs_plan`.
- A single giant project that just renames the goal. If the goal doesn't
  actually decompose into distinct projects, say that explicitly instead of
  forcing an artificial split.
