# Evaluation Session — Type Prompt
# Injected into the <GOAL> block when session_type=evaluation

This is an evaluation session. A goal or project is waiting to be vetted before
it becomes real work.

## Mode: EVALUATION

Something entered the Loom queue as a `desire` -- an idea or direction that hasn't
been scoped, planned, or committed to yet. It can be a goal or a project; this
rule is level-agnostic, and a project-level desire always gets checked against
the goal it's meant to serve, not judged standalone. Your job this session is
to decide whether it's ready to move forward, not to start building it.

## How to proceed

1. Read the `## TARGET` block above -- it names the specific desire-status goal
   or project this session is evaluating, with its id, status, priority, and
   description. That's the record to act on, not whatever
   `state/loom_context.json`'s active goal happens to be -- this rule runs
   system-wide, so the matched record may not be the active one.
2. If it references other files (design docs, prior discussion), read those too.
3. If evaluating a **project**, also read its parent goal (goal_id in the
   target detail) -- a project can't be judged in isolation from what it's
   supposed to serve.
4. Assess:
   - **Scope**: is this actually one goal/project, or several wearing one name?
   - **Feasibility**: is this achievable with what exists today, or does it depend
     on something not yet built or decided?
   - **Fit**: for a goal, does it belong alongside the other active goals, or
     duplicate one? For a project, does it genuinely serve its parent goal?
5. Decide one of the outcomes below. Do not skip the decision -- an evaluation
   session that leaves a goal or project exactly as it found it has failed,
   unless "not ready yet" is itself the honest conclusion (state why explicitly).

## Outcomes

**Promote to `needs_plan`** -- scoped and feasible enough that a planning
session could turn it into concrete work:
```
# Goal:
PYTHONPATH=~/lain/loom ~/lain/loom/.venv/bin/python -m loom.cli \
  --db ~/.local/share/loom/loom.db goal edit <GOAL_ID> -s needs_plan

# Project — note: project edit has no short flag for status; -s means
# --start-date there, not --status. Always spell it out for projects:
PYTHONPATH=~/lain/loom ~/lain/loom/.venv/bin/python -m loom.cli \
  --db ~/.local/share/loom/loom.db project edit <PROJECT_ID> --status needs_plan
```
Also sketch what comes next so the planning session doesn't start from a blank
page: for a goal, 2-4 candidate project names; for a project, 2-4 task
skeletons (name + one-line description) in `memory/work/goal_<ID>/`, or add
them directly as Loom tasks with status `triage`.

**Leave as `desire`** -- not ready yet, but still worth keeping. State
explicitly what's missing (a decision, a dependency, more information) so
the next evaluation session doesn't repeat this one from scratch.

**Suspend** -- this shouldn't be pursued right now:
```
PYTHONPATH=~/lain/loom ~/lain/loom/.venv/bin/python -m loom.cli \
  --db ~/.local/share/loom/loom.db goal edit <GOAL_ID> -s suspended
# or: project edit <PROJECT_ID> --status suspended
```
State why, briefly, so a future session (or the owner) understands the
reasoning without re-deriving it.

## What NOT to produce

- Implementation. Not even a prototype.
- A plan. That's the planning session's job, once this goal reaches `needs_plan`.

## What to produce

- A status transition for the goal or project reviewed (or an explicit,
  reasoned decision to leave it as `desire`).
- Project or task skeletons if promoting to `needs_plan`.
- One line in `memory/latest_summary.md` noting what was evaluated and the outcome.
