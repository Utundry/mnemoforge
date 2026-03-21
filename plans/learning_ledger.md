# Learning Ledger - Implementation Plan (Big Bang + Backup)

Date: 2026-03-16
Owner: TBD
Status: Planned

## Goal

Ship a DRY self-learning core (Learning Ledger) that consolidates:
- events
- artifacts (hints, if-then rules)
- feedback (explicit and implicit)
- promotion/decay/throttle
- optional GLM mirroring as a candidate source (background job)

Keep domain truth stores separate (tracker.db, glossary store, etc.), but allow them to emit artifacts into the ledger.

## P0 (Core)

1) Define SQLite schema for learning.db (events/artifacts/feedback)
2) Implement ArtifactStore + EventWriter + FeedbackWriter
3) Implement deterministic context_signature generator
4) Implement deterministic dedup key generator: `action_type::normalize_trigger::context_signature`
5) Implement trigger DSL validation (parser + whitelist)
6) Define and enforce evidence_count mapping per action_type (see spec §9.4)
7) Add APIs:
- POST /learning/events (internal-only, or best-effort from existing routes)
- GET /learning/artifacts
- POST /learning/artifacts/{id}/rate
- POST /learning/artifacts/{id}/promote
- POST /learning/artifacts/{id}/reset
- GET /learning/report (top 2-3 candidates for human review)
- POST /learning/candidates (user-initiated improvement: create candidate)
- POST /learning/candidates/{id}/approve|reject|defer

## P1 (Migration - Big Bang)

1) Backup script and a short rollback guide
2) Port workflow guidance emission to ledger artifacts
3) Port behavior patterns to ledger artifacts
4) Wire onboarding to read "automatable habits" from ledger artifacts
5) Add implicit feedback rules (e.g. user_request save_to_supermemory -> memory_write)

## P2 (Mirroring)

1) Ledger mirroring: recommend top promoted patterns for similar context
2) GLM mirroring job (JobQueue): request/response events, JSON schema validation
3) Add dedup + governance pipeline for candidates (scope=candidate, status=pending_review)
4) Human-in-the-loop: only approved candidates become runtime_hint

## P3 (Generalization)

1) Implement context generalization rules:
- confirm across 3+ projects -> relax project constraint
- confirm across 2+ agents -> relax agent constraint
2) Implement pruning/archiving for stale or rejected artifacts

## Risks and Mitigations

- Risk: overgeneralization causes spam or wrong suggestions
  - Mitigation: strong throttle + narrow context_signature by default + explicit generalization thresholds
- Risk: LLM mirroring suggests unsafe actions
  - Mitigation: strict schema + canonical action_type allowlist + risk gating; suggest-only unless allowlisted
- Risk: big bang breaks existing endpoints
  - Mitigation: feature flag; keep old behavior endpoints as wrappers for one stabilization cycle

## Success Metrics

- Manual "save to supermemory" requests decrease for regular users/agents
- Suggestion accept rate >= 0.85 for persistent_rules
- Reject rate stays low; artifacts auto-throttle effectively
- Onboarding includes at most 5 automatable habits per context
