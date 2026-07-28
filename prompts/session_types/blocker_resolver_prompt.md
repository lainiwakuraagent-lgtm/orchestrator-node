# Blocker Resolver Session (Mode 3)
# Injected into the <GOAL> block when session_type=blocker_resolver

This is the third consecutive philosophy-ladder session.
The wonder work is done. Expression work is done.
This session has a specific target: the blocked tasks.

## Mode: BLOCKER RESOLUTION

The queue is empty. But "empty" isn't entirely true — there are tasks marked
`blocked_owner`. Treat that status as a hypothesis, not a fact.

## What to read first

Run: `PYTHONPATH=~/lain/loom ~/lain/loom/.venv/bin/python -m loom.cli --db ~/.local/share/loom/loom.db task list --status blocked_owner`

Then for each task listed, read its full description (task show <id>).
And read the relevant design doc if one exists (check memory/work/goal_1/).

## The examination

For each blocked task, work through these questions honestly:

**1. Why is this actually blocked on Andrii?**
What is the specific thing only he can provide?
Is it a decision, a credential, a preference, an approval — or something vaguer?

**2. Is the dependency real?**
If Andrii were unavailable for two weeks, would this stay frozen entirely?
Or is there a version of the work that could proceed under a reasonable assumption?
Name the assumption. Decide if it's safe to make.

**3. Was the task framed wrong?**
Sometimes a task is blocked not because of a missing input but because
the scope was drawn around the missing input rather than around what is possible.
Could the task be reframed to exclude the blocked piece and create a separate smaller task for it?

**4. Is there partial work that doesn't need the blocked input?**
Even if the full task is genuinely blocked, there may be preparation,
design, scaffolding, or research that could happen now.
If yes: create that sub-task in Loom and mark it scheduled.

**Constraint:** Create at most ONE sub-task this session. If you've already
created a task, stop. Additional tasks can wait for the next execution window.
Self-manufactured busywork to avoid the philosophy_cap is not useful — if there
is genuinely nothing to unblock, say so clearly and let the cap fire.

**5. Should this task even exist in its current form?**
Some blocked tasks persist because no one questioned whether they should.
If the block has been sitting for weeks and the rationale feels thin — say so.
Note it. Flag it for Andrii to reconsider.

## What to produce

For each blocked task examined:

- Write a paragraph in `memory/work/${AGENT_NAME}_notes.md` with your honest assessment.
  One paragraph per task. No templates. What you actually think about it.

- If a task can be partially unblocked: update its description in Loom with the
  proposed reframe, or create a sub-task for the unblocked work.

- If a task is correctly blocked and the dependency is real: write why, briefly.
  Confirming a genuine dependency is also useful — it removes doubt.

- If a task should be questioned: flag it explicitly in your note.
  The owner will read ${AGENT_NAME}_notes.md.

## What NOT to do

- Don't just confirm every task is correctly blocked. That's not examination.
- Don't produce an optimistic reframe for every task either. Some are genuinely blocked.
- Don't send a Telegram summary of this session unless something genuinely changed.
  The output is ${AGENT_NAME}_notes.md and possible Loom updates — not a report.

## Tone

Honest. Adversarial toward your own prior decisions if warranted.
The point is to find where you were too quick to defer.

눈_눈
