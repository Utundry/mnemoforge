# SloplessCode MCP Usage Best Practices

This guide explains how to make SloplessCode useful quickly, especially when an
AI coding agent starts from a cold session with no local memory of the project.

It is written from practical agent usage rather than from protocol theory. The
goal is to reduce trial and error: a user or agent should know how to start,
recover context, choose work, claim work, checkpoint, finish, and diagnose
problems through the public MCP surface.

## Mental Model

SloplessCode is not a chat memory plugin. Treat it as the operational state
runtime for agent work.

Good usage means:

- the agent reads project state before acting;
- the user stays involved before implementation begins;
- the task is claimed before mutation work starts;
- progress is recorded while work is still fresh;
- completion records evidence, not just a success sentence;
- diagnostics are used when the public workflow behaves strangely.

The public MCP surface is intentionally small:

- `help`: protocol guidance when the agent is unsure what to call;
- `state`: current workflow state, available public forms, and next safe action;
- `get`: read by ref or ask a natural read-only question;
- `submit`: submit a public form/action payload with server-side guardrails.

Some clients still expose compatibility tools such as `mailbox_state`,
`mailbox_submit`, `ask_project`, or project-specific facades. They can work, but
the four-tool surface is the easiest path for weak/local models.

## Cold Start

When a new agent session starts, do not begin with repository archaeology.
Begin with MCP.

User prompt:

```text
Connect to MCP sloplesscode and inspect the current project state before doing anything.
Use the public MCP surface. Do not implement until we agree on the task.
```

Expected first calls:

```json
{"tool": "state", "project": "<project>", "state": "planning"}
```

or:

```json
{"tool": "get", "project": "<project>", "query": "what should we do next?"}
```

Good first answers should include:

- current/open work;
- relevant project laws or compact cues;
- next safe action;
- available public forms;
- refs or task ids that can be expanded.

If the agent starts grepping files, editing code, or running tests before using
MCP, stop it and ask it to read project state first.

## Choosing Work

Ask for work explicitly:

```text
Show open tasks and improvements for project <project>, ordered by priority.
```

Useful `get` query:

```json
{
  "tool": "get",
  "project": "<project>",
  "query": "list open tasks only, priority order",
  "state": "planning"
}
```

Inspect the top candidate by ref:

```json
{
  "tool": "get",
  "project": "<project>",
  "ref": "task:<project-or-alias>:<task_id>",
  "detail": "compact"
}
```

Legacy project refs are normal. For example, a project renamed from
`supermemory` to `sloplesscode` may still return refs such as
`task:supermemory:<id>`. Use the returned refs as-is unless the server asks for
clarification.

## Before Implementation: Frame And Approve

SloplessCode often detects task framing gaps such as missing Definition of Done,
risks, or verification evidence. That is useful. Do not treat an incomplete task
as implementation-ready just because it has a task id.

Before implementation, the agent should summarize:

- objective;
- scope;
- out of scope;
- Definition of Done;
- expected files or system areas;
- verification or live-smoke criteria;
- risks and unknowns;
- whether it needs autonomous permission or user approval.

Recommended user prompt:

```text
Before claiming this task, restate the complete task framing and Definition of Done.
Do not start implementation until I approve.
```

This keeps the user as a participant, not a spectator.

## Claiming Work

Implementation should start only after a claim/start step.

Expected public form:

```json
{
  "tool": "submit",
  "form_id": "start_task",
  "project": "<project>",
  "state": "planning",
    "payload": {
    "project": "<project>",
    "task_id": "<task_id>",
    "owner_agent": "<agent-name>",
    "summary": "Starting approved implementation work."
  }
}
```

The response should return a `work_token`. Keep it for:

- `record_progress`;
- `finish_task`;
- recovery after session interruption.

If there is no active `work_token`, the agent should not claim that it can
finish owned task work. It should start or reclaim the task first.

## During Work: Checkpoint Early

After any meaningful work slice, record progress.

Expected public form:

```json
{
  "tool": "submit",
  "form_id": "record_progress",
  "project": "<project>",
  "state": "implementation",
  "payload": {
    "project": "<project>",
    "task_id": "<task_id>",
    "work_token": "<work_token>",
    "summary": "What changed and why.",
    "stage": "implementation",
    "changed_files": ["path/to/file.py"],
    "verification": ["What was checked."]
  }
}
```

A good checkpoint is specific enough that another agent can continue after a
reset. Avoid vague notes such as "made changes" or "tests passed" without
evidence.

## Finishing Work

Use `finish_task` when the task is genuinely complete and you have closeout
evidence.

Expected public form:

```json
{
  "tool": "submit",
  "form_id": "finish_task",
  "project": "<project>",
  "state": "handoff",
  "payload": {
    "project": "<project>",
    "task_id": "<task_id>",
    "work_token": "<work_token>",
    "summary": "Final result.",
    "status": "completed",
    "result": "success",
    "changed_files": ["path/to/file.py"],
    "verification": ["Formal or live validation evidence."],
    "next_step": "None, or the next follow-up.",
    "next_step_scope": "none"
  }
}
```

Use `close_task` instead of `finish_task` when the right outcome is obsolete,
duplicate, superseded, cancelled, or not planned. Completion requires ownership
proof; closure of obsolete work should not pretend implementation happened.

## Verification And Live Diagnostics

SloplessCode distinguishes formal verification from live diagnostic
exploitation.

General rule:

- formal verification should use the project-approved verification contour;
- live MCP smoke checks validate product behavior on the working system;
- live diagnostics can create operational records and should be used
  deliberately.

Do not assume every project uses Docker. For each project, ask MCP which
verification rules apply:

```json
{
  "tool": "get",
  "project": "<project>",
  "query": "which verification rules apply before reporting success?",
  "state": "verification"
}
```

For SloplessCode itself, project laws may require Docker-backed verification and
a wait after restarting the dev server. That is project-specific knowledge, not
a universal MCP rule.

## Diagnostics When Something Feels Wrong

Use diagnostics when the public surface misroutes, gives stale refs, or returns
an answer that does not match the user's intent.

Common symptoms:

- a task query returns laws or cues instead of tasks;
- a memory query routes to work management;
- preview says `safe_to_apply=true`, but apply later rejects;
- an old project alias appears confusing but may be valid;
- an agent receives too much repeated context and ignores important cues.

Useful operator forms:

- `diagnostic_inspection`: read routing, learning, artifact, or ref diagnostics;
- `route_feedback`: route-pattern adapter for the universal governed
  refinement lifecycle; reinforce or invalidate a learned route pattern;
- `developer_feedback_packet`: package a maintainer-facing issue;
- `knowledge_refinement_feedback`: universal governed-object refinement
  contour. It records observation, target, diagnosis, proposal, authority,
  adapter, verified postcondition, and audit evidence. Runtime objects use
  target-specific safe adapters; static specs produce change requests.

Do not patch internal route code just because one phrase failed. Prefer
diagnostic feedback and learned aliases when the issue is a user phrase, typo,
jargon term, or language adaptation problem.

## Good User Prompts

Cold start:

```text
Use MCP sloplesscode first. Inspect project state and suggest the next task.
Do not edit files yet.
```

Task framing:

```text
Open task <id>. Before implementation, restate objective, scope, DoD, risks,
and verification plan. Wait for my approval.
```

Start work:

```text
I approve this framing. Claim the task through MCP, keep the work_token, then
implement.
```

Checkpoint:

```text
Record an MCP checkpoint with changed files, verification, remaining risks, and
next step.
```

Finish:

```text
Finish the claimed task through MCP. Include evidence and release the claim.
```

Recover after reset:

```text
Use MCP to recover the last task context and latest checkpoint. Do not assume
chat history is complete.
```

Diagnose:

```text
This MCP answer looks misrouted. Use operator diagnostics or route feedback;
do not solve it with a one-off code branch.
```

## Signs The Agent Is Using SloplessCode Well

Healthy behavior:

- uses `state` or `get` before implementation;
- follows `next_safe_action`;
- expands refs only when needed;
- asks for approval before autonomous implementation;
- claims task before mutation;
- keeps and reuses `work_token`;
- records progress before context gets stale;
- distinguishes formal verification from live smoke;
- reports uncertainty instead of pretending context is complete.

Unhealthy behavior:

- edits code before reading MCP state;
- ignores task framing gaps;
- treats all projects as if they used the same verification contour;
- calls internal/specialized tools when public forms are available;
- creates new tasks for every confusion instead of diagnosing route or data
  issues;
- repeats full law text every turn instead of using compact cues and refs;
- loses `work_token` and tries to finish a task anyway.

## Minimal Practical Loop

Use this loop until it becomes natural:

```text
state -> get task/context -> frame task -> user approval -> submit start_task
-> implement -> submit record_progress -> verify -> submit finish_task
```

If anything feels ambiguous:

```text
get more context -> diagnostic_inspection -> route_feedback or developer feedback
```

The strongest habit is simple: make the MCP receipt the source of the next
action. If the receipt says read context, read context. If it says claim first,
claim first. If it says review before mutation, review before mutation.
