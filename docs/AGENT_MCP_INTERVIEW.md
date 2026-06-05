# Agent Interview: Working Through SloplessCode MCP

This is a short field interview with Codex as an AI coding agent actively using
SloplessCode MCP during real development of SloplessCode itself.

It is not a benchmark. It is a practical report about how the workflow feels
from the agent side compared with ordinary stateless agent development.

## Interview

**Q: What changes when you work through SloplessCode MCP instead of a normal
agent chat?**

The biggest change is that I stop treating the repository and the current chat
as the only source of truth. In an ordinary session, I have to reconstruct the
project from files, logs, previous messages, and user reminders. That works for
small tasks, but it degrades badly as the project grows.

With SloplessCode, I can ask for project state, task context, rules, checkpoints,
and next safe action through MCP. That gives me an operational map before I
touch code.

**Q: Does it save tokens?**

Yes, in practice. The saving is not just from shorter answers. The real saving
comes from avoiding repeated reconstruction.

Without MCP, I often need to read README files, scan docs, grep code, inspect
tests, and infer what happened in the last session. With MCP, a compact task
context or checkpoint can replace much of that. The exact ratio depends on the
project, but the pattern is consistent: session start, recovery after reset, and
task continuation become much cheaper.

The important caveat is that compactness must not hide critical information. The
best responses are not the shortest responses; they are the responses that give
enough context to ask the right next question.

**Q: What is the main difference for long sessions?**

Ordinary long agent sessions tend to accumulate drift. The model starts well,
then gradually loses track of rules, old decisions, task boundaries, and why a
piece of code exists. Past a certain project size, the agent can fall into slop:
duplicated abstractions, inconsistent fixes, confident but stale assumptions,
and patches that solve symptoms instead of causes.

SloplessCode reduces that failure mode because task state, rules, checkpoints,
and decisions are externalized. If the session gets long, resets, or switches
models, the next agent can recover from MCP instead of pretending it remembers.

**Q: What helps the user most?**

The user can work at the level of ideas and corrections instead of constantly
reconstructing context for the model.

Good MCP usage lets the user say things like:

- "show the next priority";
- "open this task";
- "fix the missing DoD";
- "remember this rule";
- "checkpoint progress";
- "finish the task with evidence".

That is very different from repeatedly pasting project history into a prompt.

**Q: What does SloplessCode do better than plain memory?**

Plain memory stores facts. SloplessCode stores operational state.

For coding work, that distinction matters. A fact like "this project uses a
Docker test contour" is useful, but a task also needs ownership, a work token,
stage, checkpoint, changed files, verification evidence, next step, and closure
state. SloplessCode connects those pieces into a workflow.

**Q: Does the system get better over time?**

Yes. A useful SloplessCode installation should become more effective as it works
with a specific user and project.

The system can learn more than routes. It can accumulate aliases, recurring
phrases, common typos, project laws, development standards, verification habits,
and feedback about which responses were helpful or noisy. That matters because
real users do not always speak in formal API terms. They use their own language,
shortcuts, jokes, local terminology, and sometimes the wrong keyboard layout.

In this development session, that showed up in several practical ways:

- Russian user phrases such as "приступай" are treated as user-language aliases
  for an internal English workflow intent, not hardcoded into static specs.
- The user repeatedly reinforced the principle "private case through general
  mechanism"; that became an architectural constraint for future changes.
- Docker verification, live diagnostic usage, DockerHub publishing, and MCP
  task ownership became governed project habits rather than reminders pasted
  into every prompt.
- Repeated friction around noisy responses led to compact cue markers,
  stage-aware guidance, and expand refs instead of full context every turn.
- Misroutes and confusing results became candidates for route feedback,
  diagnostics, or knowledge refinement rather than one-off code patches.

That is the long-term value: the system can adapt to the user's way of working
without turning every preference into hardcoded router logic. Static specs hold
basic instincts; the live database holds acquired habits and project standards.

**Q: What makes the system feel safe to use?**

The useful part is not that the agent becomes fully autonomous. It is almost the
opposite: the workflow makes boundaries visible.

Before implementation, the agent should frame the task and wait for approval.
Before mutating task state, it should claim the task and keep the work token.
Before reporting completion, it should record evidence. If something looks
misrouted or stale, the system can route that into diagnostics or feedback
instead of burying it in chat.

That makes the user a participant in the development process rather than a
spectator watching an agent run ahead.

**Q: What is still imperfect?**

The biggest remaining challenge is noise versus guidance. Agents do not need the
full text of every law on every turn, but they do need the right reminders at
the right stage. Compact cue markers, expand refs, and stage-aware guidance are
the right direction.

Another challenge is data hygiene. Legacy records, test garbage, stale facts,
and old project aliases can all pollute search and routing. The system needs to
keep improving its self-cleaning and diagnostic loops.

**Q: Would you recommend working through SloplessCode MCP?**

Yes, especially for projects that are too large or too long-lived for one chat
session to hold coherently.

For a tiny one-off script, plain chat is often enough. For a real project with
rules, tasks, verification, interruptions, model switches, and evolving
architecture, I would rather work through SloplessCode than without it.

The reason is simple: without operational continuity, the agent eventually has
to guess. With SloplessCode, it can ask the system what it is doing, why, what
rules matter, and what the next safe action is.
