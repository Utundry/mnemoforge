# Agent Interview: A Real SloplessCode MCP Work Session

This document is a field interview with Codex, written as a typical work session
rather than a detached Q&A. It describes how SloplessCode MCP feels from the
agent side while the agent is working on SloplessCode itself.

It is not a benchmark and not a marketing transcript. It is a compact report of
the habits that emerged in real development: ask MCP first, frame the task,
wait for approval, claim the work, make a focused change, verify on the right
contour, checkpoint, commit, and close out.

## Session Snapshot

**Работал на протяжении 7m 29s**

**User:** Choose the next priority task.

**Codex:** I ask MCP first instead of guessing from chat memory. The current
planning packet gives me project laws, the workflow state, and the next safe
action. Then `project_work` selects the next open task:

`da9098a4-2b86-4d68-b349-65f628992ce7`
`Honor explicit confirmed target status in project_rules`

The important part is not just the task ID. MCP also reminds me that project
rules are data, not hardcoded behavior, and that technical ability to edit is
not authority to edit.

**User:** Formulate it.

**Codex:** I fetch the task context through the public mailbox form before
inventing a plan. The context says the task is not fully framed: it needs a
Definition of Done. I turn that into a visible task statement:

- preserve explicit law statuses such as `proposed`, `user_confirmed`, and
  `active`;
- keep the safe default when no explicit status is supplied;
- use existing confirmation metadata for confirmed or active laws;
- avoid project-specific hardcoding;
- add focused Docker tests;
- do not write-test the working SQLite database.

Then I stop. This is the approval boundary.

**User:** Start implementation.

**Codex:** Now I submit `start_task`. MCP gives me a work token, a lease, a
work session, and explicit edit authority for exactly the approved framing. If
the goal or solution direction changes, that approval no longer applies.

Only after that do I touch code.

## What Changed Compared With Ordinary Agent Chat

In an ordinary agent session, the repository and current conversation become the
agent's whole world. I reconstruct context from files, logs, previous messages,
and user reminders. That can work for small tasks, but it degrades as the
project gets older.

With SloplessCode MCP, I start from operational state:

- What is the current task?
- Is it framed?
- Has the user approved this exact framing?
- Is the task claimed?
- What project laws matter at this stage?
- What verification contour is allowed?
- What is the next safe action?

That changes the texture of the work. I am not just searching for facts. I am
checking whether I have authority to act.

## The Implementation Slice

For the `project_rules` task, the code path already handled direct mailbox
`create_law` reasonably well: `active` and `user_confirmed` required
`confirmed_by`, and the status was passed to `/laws`.

The rough edge was in the thematic `project_rules` facade. A proposal-shaped
intent with a title and statement defaulted to a trial rule candidate. That is
right for ordinary "propose new law", but wrong when the operator gives an
explicit law status such as `target_status=active`.

The focused fix was:

- keep ordinary proposals on the candidate path;
- when `target_status` or `status` is one of `proposed`, `user_confirmed`, or
  `active`, route to `create_project_law`;
- pass status and confirmation metadata through the payload;
- keep mutation guarded unless `allow_mutation=true`.

This is a small example of a bigger SloplessCode lesson: lifecycle is more
important than retrieval alone. The difference between `trial`, `proposed`,
`user_confirmed`, and `active` is not UI decoration. It is part of the truth of
the rule.

## Verification

I did not run the whole test suite. That would have been slow and noisy for a
small routing change. The project habit is focused verification:

```text
.\scripts\run_pytest_docker.ps1 -NoBuild -q tests\test_mcp_sse.py -k "..."
```

The relevant tests passed:

```text
7 passed, 337 deselected
```

Then I restarted the dev MCP container so the practical contour used the new
code, and checked planned-mode behavior without writing to the working
database:

- `target_status=active` routed to `create_project_law`;
- default `propose new law` still routed to `create_rule_candidate`;
- both were `executed=false` with guardrails active.

That distinction matters. SQLite is the source of truth. Semantic search and
Qdrant are indexes over that truth, not places to casually test writes. Live
checks are useful, but they must be labeled as diagnostic/operator feedback, not
formal code verification.

## Checkpoint, Commit, Closeout

After the meaningful slice, I recorded progress through MCP with the work token:

- changed files;
- verification evidence;
- next step;
- stage.

Then I committed the two-file change:

```text
fix(mcp): honor explicit project rule status
```

After push, I closed the task with `finish_task` in the mailbox checkpointing
state. The closeout recorded the result, evidence, commit, and released the
claim.

This is the part I would miss without SloplessCode. Not search first:
continuity. An external system knows the task, the claim, the evidence, and
what is safe to do next.

## Agent Notes

**What does MCP change most?**

It makes the collaboration boundary visible. "Continue" is not the same as
permission to edit anything. A user-approved framing is narrow. If the cause,
solution direction, affected subsystem, risk, or Definition of Done changes, I
should stop and reframe.

That is especially useful for strong models. Weak models often fail visibly:
wrong tool, missing field, bad parse. Strong models can fail fluently. They can
infer too much, start early, or present a plausible action as if it were the
user's decision. MCP gives those behaviors something to collide with.

**Does it save tokens?**

Yes, but the real saving is not just shorter answers. The saving comes from not
reconstructing the whole project every time. A compact task packet can replace a
lot of README scanning, grep, and chat archaeology.

The caveat is important: compactness must not hide critical information. The
best MCP response is not the shortest one. It is the smallest response that
still lets the agent choose the right next action.

**What does SloplessCode do better than plain memory?**

Plain memory stores facts. SloplessCode stores operational state.

A fact like "tests run in Docker" is useful. A working task also needs a claim,
a work token, stage, checkpoint, changed files, verification evidence, next
step, and closure state. SloplessCode connects those pieces into a workflow.

**Does the system get better over time?**

It can, when its mistakes are turned into structured input. Misroutes become
route feedback or route hygiene findings. Noisy responses become compact cue
work. Missed approval boundaries become project laws and workflow guardrails.
Old project names become aliases instead of broken lookups. Keyboard-layout
mistakes and user-language phrases can be learned through data, not hardcoded
into static routing logic.

That is the point of using the project to develop itself: self-reference is not
permission to special-case the project. The same memory, improvement, law, and
verification loops should improve SloplessCode that it offers to other
projects.

**What is still imperfect?**

Noise versus guidance remains hard. Agents do not need every law in full on
every turn, but they do need the right cue at the right stage. Compact markers,
expand refs, and stage-aware packets are the right direction.

Data hygiene is the other long-running challenge. Legacy records, stale routes,
test garbage, old project aliases, and obsolete guidance can all pollute search
and judgment. The system needs diagnostic loops that make those problems visible
before they become repeated agent failures.

**Would I rather work with MCP or without it?**

For a tiny one-off script, plain chat is often enough. For a real project with
rules, tasks, verification, interruptions, model switches, and evolving
architecture, I would rather work through SloplessCode MCP.

Without operational continuity, I eventually have to guess. With SloplessCode, I
can ask the system what I am doing, why, which rules matter, and what the next
safe action is.
