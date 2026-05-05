# Learning Ledger (Self-Learning Loop) - Technical Specification

Version: 0.1 (draft)
Date: 2026-03-16
Status: Proposed (design locked for MVP)

## 1. Problem Statement

The system already contains multiple self-learning loops (skills outcomes, workflow guidance, behavioral reflexes, routing tracker).
They share the same structure (observe -> suggest -> feedback -> promotion/decay) but are implemented separately.

Goal: implement one DRY, extensible "Learning Ledger" that:
- captures unified learning events
- produces unified artifacts (hints, if-then rules, meta guidance)
- learns from unified feedback
- promotes/decays artifacts with consistent governance and throttling
- supports "mirroring" as a source of candidate behavior, including an LLM option (GLM) in background jobs
- supports user-initiated improvements: a user can manually propose a candidate artifact (rule or hint) to bootstrap learning

Non-goals (MVP):
- automatically executing medium/high-risk actions
- replacing domain truth stores (tracker.db, glossary store, etc.)

## 2. Core Entities

### 2.1 Episode
An "episode" is the unit of learning and evaluation, e.g. a session, a task, a PR, or a background job.

Fields:
- episode_id: string (stable identifier across the episode)
- agent_id: string (e.g. codex, claude-code)
- project: string (e.g. mnemoforge)

### 2.2 LearningEvent (events)
Append-only events collected from runtime:
- user requests
- tool calls and results
- memory writes
- artifact suggestions and feedback
- episode start/end
- LLM mirroring requests/responses

### 2.3 Artifact (artifacts)
An Artifact is something the system may suggest or (if allowed) execute.
Artifact types (MVP):
- hint: a suggestion message + recommended action
- if_then_rule: a deterministic rule that maps observed triggers to an action_type
- meta_guidance: suggestions about the learning process itself (throttle, ask for feedback, reduce noise)

Lifecycle (scopes):
- candidate: produced by miners (rule-based or GLM) and awaiting human review
- runtime_hint: ephemeral, low commitment
- persistent_rule: validated enough to keep and show across episodes
- promoted_pattern: high-confidence rule/pattern, eligible for broader generalization

### 2.4 Feedback (feedback)
Unified feedback signals:
- explicit: accept/reject, useful/not_useful
- implicit: inferred acceptance from subsequent actions
- outcomes: success/fail/latency/corrections (used by domain adapters)

## 3. Storage Model (SQLite)

Suggested DB: qdrant_data/learning.db

Tables (MVP):
1) events
- ts (float)
- episode_id (text)
- agent_id (text)
- project (text)
- transport (text)
- event_type (text)
- context_signature (text)
- payload_json (text)

2) artifacts
- id (text, uuid)
- domain (text)
- artifact_type (text)
- action_type (text)
- key (text, dedup key)
- scope (text)
- risk_level (text)
- agent_id (text, nullable for global)
- context_signature (text)
- trigger (text, DSL for if_then_rule; empty for hint)
- payload_json (text)
- evidence_count (int)        # miner-provided or computed evidence supporting this artifact
- confidence (real)
- accepts (int), rejects (int), useful (int), not_useful (int)
- cooldown_s (int), last_emitted_ts (real)
- status (text: active|pending_review|disabled|archived)
- created_at (real), updated_at (real)

3) feedback
- ts (float)
- episode_id (text)
- artifact_id (text, nullable)
- valence (text: positive|negative)
- magnitude (real: 0..1)
- source (text: user|agent|system)
- payload_json (text)

## 3.1 Artifact Dedup Key

The `key` field is a deterministic string used for deduplication and throttling.

Formula:
```
key = "{action_type}::{normalize_trigger(trigger)}::{context_signature}"
```

`normalize_trigger`:
- sort all conditions alphabetically
- strip whitespace
- lowercase values
- empty trigger → empty string (for hints without trigger)

Example:
```
auto_save_result::event(user_request).request_type=="save_to_mnemoforge"::category=code;phase=implement;project=mnemoforge
```

Two candidates with the same key are considered duplicates. The one with higher evidence_count wins; the other is archived.

## 4. Canonical Context

context_signature is a deterministic key used for:
- deduplication and aggregation
- throttling (avoid spam)
- generalization (relax constraints over time)

Canonical fields (MVP):
- project
- task_type
- phase: plan|implement|test|wrap_up|unknown
- category
- transport: mcp|http|cli|internal
- agent (optional; only for agent-specific reflexes)

Normalization:
- sort keys
- use "unknown" for missing values
- format: key=value;key=value;...

## 5. Canonical Learning Events (MVP)

All events share an envelope:
- ts, episode_id, agent_id, project, transport, event_type, context_signature, payload

Event types (MVP set = 10):
1) episode_start
2) episode_end
3) user_request
4) user_feedback
5) tool_call
6) tool_result
7) memory_write
8) artifact_suggested
9) artifact_feedback
10) llm_mirror

See also: internal events can be added later, but must map to the same schema.

### 5.1 User-initiated improvements (event mapping)

User-initiated improvement proposals should be recorded as `user_request` events to keep the canonical `event_type` set small.

Recommended encoding:
- `event_type=user_request`
- `payload.request_type=other`
- `payload.proposal_type=learning_candidate`
- `payload.proposal` contains the proposed candidate fields (`artifact_type`, `action_type`, `trigger` or `payload`, `context_signature`)

## 6. Canonical request_type (MVP)

Used in user_request.payload.request_type:
- save_to_mnemoforge
- run_tests
- create_improvement
- rebuild_docs
- summarize
- explain
- show_status
- other

## 7. Canonical action_type (MVP)

Behavioral actions:
- auto_save_result
- suggest_save_result
- run_tests
- suggest_run_tests
- create_improvement
- suggest_create_improvement
- rebuild_docs
- suggest_rebuild_docs
- request_missing_info
- switch_to_background_job

## 8. Trigger DSL for if_then_rule (MVP)

Artifacts of type if_then_rule must be deterministic and testable.

Trigger grammar (minimal):
- event(TYPE)
- event(TYPE).field == "value"
- event(TYPE).field in ["a","b"]
- not event(TYPE)
- within(SECONDS, PREDICATE)

Allowed TYPE: user_request, user_feedback, tool_call, tool_result, memory_write, episode_end, artifact_suggested

Allowed fields are whitelisted per TYPE; no arbitrary JSON access in MVP.

Example:
- event(user_request).request_type == "save_to_mnemoforge" and not event(memory_write)

## 9. Learning Policies (MVP)

### 9.1 Confidence models
Per artifact_type:
- if_then_rule / behavior reflexes: Laplace-smoothed accept rate
- hints with voting: simple votes -> confidence mapping
Domain adapters may keep Wilson scoring (routing), but should emit artifacts to the ledger.

### 9.2 Promotion / decay
- candidate -> runtime_hint (human-in-the-loop):
  - candidate appears only after evidence_count >= N (per action_type policy)
  - surfaced via GET /learning/report with at most 2-3 items (avoid overload)
  - user approves/rejects/defers candidates; miners never auto-approve
- runtime_hint -> persistent_rule:
  - accepts >= 5
  - confidence >= 0.85
  - rejects not increasing recently (optional)
- persistent_rule -> promoted_pattern:
  - useful votes >= 3 OR accept_rate >= 0.9
  - evidence across >= 2 episodes
- decay:
  - repeated rejects -> disabled
  - long inactivity -> archived

### 9.3 Throttling
- per artifact.key: do not emit more than once per 30 minutes per agent
- global per key: no more than once per 2 hours

### 9.4 Evidence Count Mapping

`evidence_count` defines how many corroborating events are required before a candidate is surfaced for review.
Each action_type has a minimum threshold (configurable; these are defaults):

| action_type | counting event_type | min_evidence |
|---|---|---|
| auto_save_result | memory_write (source=auto) | 5 |
| suggest_save_result | user_request(save_to_mnemoforge) without memory_write | 3 |
| run_tests | tool_call(run_tests) after code change | 4 |
| suggest_run_tests | episode_end without tool_call(run_tests) | 3 |
| create_improvement | tool_call(report_issue) or artifact_suggested(improvement) | 3 |
| rebuild_docs | tool_call(docs_rebuild) after skill_publish | 4 |
| suggest_rebuild_docs | episode_end with memory_write without docs_rebuild | 3 |
| request_missing_info | user_feedback(negative) after tool_result(empty) | 3 |
| switch_to_background_job | tool_call duration > 30s repeated | 4 |

Rules:
- Only events within the same `context_signature` window count.
- Events older than 30 days do not count (configurable: `evidence_window_days`).
- GLM mirror must declare which event_types it counted as evidence in candidate payload.

### 9.5 Governance (auto-execution)
Auto-execution allowlist (MVP):
- auto_save_result
- rebuild_docs (optional, still low-risk)

Everything else is suggest-only.
High-risk actions are never auto, regardless of history.

## 10. Mirroring Sources

"Mirroring" produces candidate artifacts. It does not execute them.

### 10.1 Ledger mirroring
Select top promoted artifacts for a similar context_signature, ranked by confidence and usefulness.

### 10.2 LLM mirroring (GLM background job)
Job: llm_mirror
Input:
- context_signature
- recent events summary
- active artifacts (to avoid duplicates)
Output:
- strictly JSON candidates mapping to canonical action_type + trigger DSL (or hint payload)

All LLM candidates must pass:
- schema validation
- risk gating
- dedup by key

Governance rule:
- LLM mirroring produces candidates only (status=pending_review, scope=candidate).
- Candidates become runtime_hint only via explicit human approval.

## 10.3 Human-in-the-Loop Review API (MVP)

To prevent noisy or unsafe automation, the system exposes a small review surface.

Endpoints (proposed):
- GET /learning/report
  - returns top 2-3 candidates ranked by confidence * evidence_count
  - each item includes: observation, why_it_matters, proposed_rule, confidence, risk, evidence_count
- POST /learning/candidates
  - user-initiated improvement: create a candidate artifact directly (scope=candidate, status=pending_review by default)
  - records a corresponding `user_request` event for auditability
- POST /learning/candidates/{id}/approve
  - sets status=active, scope=runtime_hint (or persistent_rule if explicitly forced later)
- POST /learning/candidates/{id}/reject
  - sets status=archived or disabled; records negative feedback
- POST /learning/candidates/{id}/defer
  - keeps status=pending_review
  - raises the candidate's `min_evidence` threshold by +3 (so it must accumulate more corroborating events)
  - sets `next_surface_after = now + defer_days` (default: 7 days); candidate is excluded from GET /learning/report until then
  - body (optional): `{"defer_days": 14, "reason": "too early to decide"}`
  - repeated defer: each subsequent defer doubles the waiting period (7 → 14 → 28 days) up to a max of 90 days, after which the candidate is auto-archived

## 11. Big Bang Migration Plan (Dev Stage)

We prefer a big bang migration to remove duplicated logic quickly, with a rollback via backups.

Pre-migration backup targets:
- qdrant_data/adaptive_state.db
- qdrant_data/tracker.db
- qdrant_data/jobs.db
- qdrant_data/capabilities.json
- qdrant_data/docs_cache/*

Migration steps (high level):
1) Introduce learning.db schema and engine
2) Port workflow guidance artifacts to ledger
3) Port behavior patterns to ledger
4) Update get_onboarding to read automatable habits from ledger
5) Keep old endpoints for a short stabilization window; disable writes or route them into the ledger
6) Add a rollback doc: restore backed up files and disable ledger feature flag

## 12. Acceptance Criteria (MVP)

- Unified list/promote/rate/reset endpoints work for both hint and if_then_rule.
- Auto-save reflex can be learned from events without manual behavior/record calls.
- LLM mirroring can create candidates; governance prevents unsafe auto-execution.
- Tests exist for:
  - dedup key stability
  - throttle
  - promotion/decay
  - trigger DSL parsing/validation
  - LLM candidate schema validation
