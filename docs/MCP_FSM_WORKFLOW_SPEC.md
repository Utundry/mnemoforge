# MCP FSM Workflow Spec

MnemoForge MCP should be data-driven workflow execution, not workflow data
hardcoded through router code.

The target shape is:

```text
state -> allowed transitions -> compact state packet -> agent action
```

This keeps MCP close to its original purpose: a finite-state guide for agent
work that exposes only the tools and data needed at the current moment.

## First Slice

This branch adds a declarative skeleton before changing runtime behavior:

- Executable workflow state specs live in `app/mcp_specs/states/*.json`.
- Public mailbox form specs live in `app/mcp_specs/forms/*.json`.
- Mailbox protocol spec lives in `app/mcp_specs/mailbox/protocol.json`.
- LLM-facing packet templates live in `app/mcp_specs/packets/*.md`.
- Clerk anchor guidance lives in `app/mcp_specs/clerk/anchor_tags.md`.
- Clerk capture type specs live in `app/mcp_specs/clerk/capture_types.json`.
- Agent-controlled feature gates live in `app/mcp_specs/features/toggles.json`.
- Runtime identity and CLI/model fingerprint specs live in
  `app/mcp_specs/identity/runtime_profile.json`.
- Task lease reclaim policy lives in `app/mcp_specs/leases/task_reclaim.json`.
- Response visibility policy lives in `app/mcp_specs/responses/envelope.json`.
- `app/services/mcp_workflow_specs.py` loads and validates these files.
- `app/services/mcp_mailbox.py` builds the first read-only public mailbox state
  packet from specs.
- `mailbox_state` is the first external read-only MCP entrypoint backed by the
  mailbox packet builder.
- `mailbox_submit` accepts one public form and returns a public receipt. During
  the migration slice it validates forms, executes the first governed write form
  (`create_improvement`) through a server-internal route, and keeps unsupported
  write forms behind review receipts.
- `mailbox_get` fetches public packets by public reference, starting with
  `mailbox_state:<project>:<state>`. Unknown/internal refs return public
  not-found receipts.
- `set_feature_gate` is a mailbox form, not a separate external tool. Agents can
  enable/disable bounded functionality through `mailbox_submit`.

JSON is used for executable specs because the project currently has no YAML
dependency. Markdown remains the format for agent-facing templates and Clerk
instructions.

## Design Principles

- `mcp_sse.py` should become transport and adapter glue, not the home of
  workflow meaning.
- External MCP should expose mailbox state/actions, not the internal tool
  catalog.
- Workflow states should declare required evidence, allowed tools, forbidden
  patterns, packet templates, and next transitions.
- Runtime code should interpret specs deterministically before consulting an
  LLM for ambiguous intent classification.
- Packet output should be compact by default and omit service/debug metadata
  unless explicitly requested.
- Verification state must surface the project-approved test contour before any
  test command can run.
- Clerk must return review packets from anchored evidence, not silently collapse
  unrelated capture into checkpoint drafts.
- Broken or experimental functionality must be disableable per session, agent,
  or project through server-side feature gates. Disabled features should be
  removed from allowed tool packets or marked unavailable with a reason and
  replacement path.
- `agent_id` is a human-facing label, not a stable capability identity. Runtime
  feature gates should be keyed by a CLI/model fingerprint when possible.

## Migration Path

1. Keep specs read-only and validate them in focused tests.
2. Expose `mailbox_state` as a read-only external state packet entrypoint.
3. Expose guarded `mailbox_submit` and read-only `mailbox_get` as the minimal
   external mailroom protocol.
4. Teach Operational Tray to load state specs for packet shaping.
5. Move hardcoded forbidden patterns and verification contours into specs.
6. Move Clerk capture classification to anchored quote specs and review packets.
7. Add the runtime feature-toggle store and mailbox form so agents can disable
   broken facades such as `project_capture` for their own session.
8. Gradually shrink `mcp_sse.py` as routing logic moves behind typed services.

## Feature Gates

Feature gates are not a replacement for fixing bugs. They are a quarantine
mechanism so an agent can keep working safely when a facade, classifier, or
helper path is known broken.

Initial gates include:

- `project_capture_facade`: disable when capture requests are being routed into
  unrelated checkpoint drafts.
- `clerk_checkpoint_draft`: disable the Clerk draft helper while preserving
  direct governed checkpointing.
- `llm_route_fallback`: force deterministic routing when lifecycle commands are
  too risky for LLM fallback.

Future runtime commands should let an agent enable or disable a gate for the
current session first, with project/global scopes requiring stronger review.

## Mailbox Forms

Mailbox forms are the public action surface. A weak agent should choose a form,
fill required fields, and submit it without knowing the internal tool route.

Initial forms:

- `get_task_context`: read compact task context before implementation.
- `create_improvement`: create backlog work with Clerk assistance and
  postconditions that forbid checkpoint-draft results.
- `record_progress`: record resumable task progress or framing evidence.
- `run_verification`: request the approved verification contour instead of
  guessing shell commands.
- `set_feature_gate`: enable or disable a feature for a bounded runtime scope.

Each form may include internal postconditions. These are used for route health
and semantic mismatch detection, but they are not included in the public packet
unless a diagnostic runtime profile explicitly requests internal metadata.

## Route Health

Every mutating internal route returns an actual metadata envelope before the
public receipt is built. The envelope uses stable fields such as:

- `result_kind`
- `artifact_type`
- `mutation`
- `review_mode`
- `internal_tool`
- `route_id`
- `command`
- `execution_context`

The mailbox layer compares this envelope with the submitted form's
`postconditions.expected_metadata` and `postconditions.forbidden_metadata`.
Mismatches produce a public `route_unhealthy` receipt and diagnostic profiles
can inspect `_internal.postcondition_health`.

## Response Visibility

The external MCP packet must be filtered before it leaves the server. Agents
should see state, instruction, allowed forms, warnings, receipts, and next safe
actions. Internal logistics must remain internal by default.

Internal metadata includes:

- route ids
- internal tool names
- scorer traces
- expected vs actual route metadata
- route health events
- raw tool payloads

Only runtime profiles with explicit diagnostic permission should receive
internal metadata, and only when diagnostics are requested. Weak and unknown
profiles should receive the smallest public packet possible.

## Runtime Identity

Many CLI agents report the same `agent_id`, such as `codex`, even when they are
different clients, models, or operational profiles. MnemoForge should separate:

- `agent_id`: human-facing logical label.
- `session_id`: short-lived MCP/SSE connection identity.
- `runtime_profile_id`: stable CLI/model/tool-competence profile.
- `agent_fingerprint`: derived fingerprint from workspace, client, and model
  hints.

The proposed workspace-local identity file is:

```text
.mnemoforge/agent_identity.json
```

This file should not be committed. It can store a local stable id and selected
runtime profile, while sensitive paths/remotes are hashed in the fingerprint.

Runtime profiles allow weaker MCP operators to receive smaller packets and have
advanced/broken features disabled by default, while stronger operators keep more
capabilities enabled.

## Task Reclaim

Stable `agent_fingerprint` should participate in task ownership. A logical
`agent_id` such as `codex` is not enough, because many CLIs and models can share
that label.

Task claims should record:

- `agent_id`
- `agent_fingerprint`
- `runtime_profile_id`
- `session_id`
- `work_token_hash`

`agent_fingerprint` is the primary ownership key. `work_id` should identify one
concrete work session/span chain, not the stable owner. This keeps identity and
activity separate: a single fingerprint can have multiple work sessions over
time, while reclaim decisions compare the stable fingerprint and use
`work_token_hash` plus audit policy as supporting evidence.

The simplified reclaim path should allow the same fingerprint to reacquire a
task after TTL expiry or session crash without using unsafe force-release flows.
It must deny reclaim when another active fingerprint owns the task, when the
incoming fingerprint is missing, or when explicit handoff moved ownership to a
different fingerprint.

Every successful reclaim should create audit evidence such as:

- `task_reclaimed_after_ttl`
- `task_resumed_after_session_loss`
- `same_agent_fingerprint_reclaim`
