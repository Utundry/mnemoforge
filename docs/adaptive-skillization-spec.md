# Adaptive Skillization — Technical Specification

**Version:** 0.1 (draft)
**Date:** 2026-03-15
**Status:** Foundation (v1 exists), this spec defines the self-improving target state.

---

## 1. Problem Statement

Current state: skill pack selection is deterministic retrieval by domain tags. Skills are static, their usefulness is never measured, and the system cannot distinguish a skill that helped from one that was ignored.

Target state: skill selection adapts over time based on observed outcomes — useful skills rise, unused ones are suppressed, gaps trigger auto-generation.

---

## 2. Core Entities

### 2.1 Skill
Already exists in Qdrant (`category="skill"`). Extended with:
- `usage_count: int` — times included in a pack
- `helpful_count: int` — times rated helpful (via outcome)
- `usefulness_score: float` — derived: `helpful_count / max(usage_count, 1)`
- `auto_generated: bool` — whether LLM-generated vs manually published

### 2.2 SkillPack
A transient bundle selected for one task. Tracked via:
- `pack_id: UUID` — unique per invocation
- `task_profile: TaskProfile` — domains + task_type
- `skill_ids: list[UUID]` — selected skills
- `phase: "immediate" | "enriched"` — delivery phase (see §4)
- `status: "delivered" | "evaluated"` — lifecycle state

### 2.3 TaskOutcome
Captured after task completion:
- `pack_id: UUID` — links to the pack used
- `skills_referenced: list[UUID]` — skills agent actually used
- `skills_helpful: list[UUID]` — skills that materially helped
- `skills_unused: list[UUID]` — selected but never referenced
- `missing_domains: list[str]` — domains where agent lacked guidance
- `success: bool` — overall task outcome

### 2.4 TaskProfile
Already exists (`POST /skills/profile`). Stable interface.

---

## 3. Architecture

```
UserPromptSubmit
      │
      ▼
┌─────────────────┐
│  Task Profiler  │  POST /skills/profile → {task_type, domains, confidence}
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Skill Selector │  GET  /skills/pack?task_tags=...  (fast, tag-based)
│  (Phase 1)      │  returns immediate pack + pack_id
└────────┬────────┘
         │ inject immediately
         ▼
    Agent runs task
         │
         ├── [async] Phase 2 enrichment (LLM scoring if pack was weak)
         │
         ▼
┌─────────────────┐
│ Outcome Tracker │  POST /skills/outcome
│                 │  records which skills helped/were unused
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Skill Evolver  │  cron job: recomputes usefulness_score per skill
│                 │  suppresses low-score, crystallizes patterns
└─────────────────┘
```

---

## 4. Two-Phase Delivery Protocol

### Phase 1 — Immediate (< 1s)
- Deterministic tag-based retrieval
- Returns `status: "immediate"`, `pack_id`, top-N skills by `importance_score * usefulness_score`
- Injected into system prompt via `UserPromptSubmit` hook

### Phase 2 — Enriched (async, 5-15s)
- Triggered if Phase 1 confidence < 0.5 OR pack size < 2
- LLM scores remaining candidate skills by relevance to task context
- OR calls `/skills/generate-for-domain` for auto-generation
- Result stored in pack record; available via `GET /skills/pack/{pack_id}`
- Future: hook can poll for enriched version and re-inject (not yet implemented)

**API contract additions:**

```
POST /skills/pack/create
  body: {task_profile, agent_id, limit}
  response: {pack_id, skills, phase, confidence}

GET  /skills/pack/{pack_id}
  response: {pack_id, skills, phase, status}

POST /skills/outcome
  body: {pack_id, skills_helpful, skills_unused, missing_domains, success}
  response: {recorded: true}
```

---

## 5. Outcome Tracking

### 5.1 Signal Sources
Signal quality ranking (best → worst):
1. **Explicit feedback** — user says "this helped" / "wrong skill"
2. **Reference detection** — agent cited skill name in response
3. **Task success heuristic** — user didn't correct/retry after response
4. **Session end** — no correction within N turns = implicit positive

### 5.2 Storage
Outcomes stored as memories:
- `category="skill_outcome"`
- `tags=["pack:{pack_id}", "skill:{skill_id}", "helpful:{true/false}"]`
- `importance_score` = signal quality (1.0 for explicit, 0.4 for heuristic)

### 5.3 Usefulness Score Update
```
usefulness_score = (
    helpful_count * quality_weight
) / (
    usage_count + smoothing_factor
)
```
where `quality_weight` ∈ [0.4, 1.0] by signal source, `smoothing_factor = 3` (Laplace).

Minimum 5 uses before score is trusted (until then, use `importance_score` as proxy).

---

## 6. Skill Evolver (Cron)

Runs daily via `POST /crystallizer/evolve` (new endpoint).

**Steps:**
1. Load all skills with `usage_count >= 5`
2. Recompute `usefulness_score` from outcome records
3. Skills with `usefulness_score < 0.2` → flag as `suppressed` (excluded from pack)
4. Detect domain gaps: domains with > 3 tasks but no skill → trigger auto-generation
5. Detect crystallization candidates: repeated successful patterns from `/crystallizer`
6. Log evolution report to memory (`category="skill_evolution_log"`)

**Safety:**
- Never delete skills automatically — only suppress
- Suppression requires `usage_count >= 10` AND `usefulness_score < 0.15`
- Human can always override via `PUT /skills/{id}` with `suppressed=false`

---

## 7. Dialogue Analyzer

**Not yet implemented.** Future component that observes the agent's response to detect:
- Which skill sections were paraphrased/followed
- Tool calls that match skill instructions
- User confirmation/correction patterns

Output: structured signal fed into `POST /skills/outcome` automatically at session end.

Implementation path: extend `Stop` hook → parse transcript → extract skill references → call outcome API.

---

## 8. API Contract (Full Target)

```
# Phase 1+2 pack delivery
POST /api/v1/skills/pack/create          → {pack_id, skills, phase, confidence}
GET  /api/v1/skills/pack/{pack_id}       → {pack_id, skills, phase, status}

# Outcome recording
POST /api/v1/skills/outcome              → {recorded: bool}
GET  /api/v1/skills/outcome/{pack_id}   → OutcomeRecord

# Evolution
POST /api/v1/crystallizer/evolve         → {suppressed, gaps_filled, crystallized}
GET  /api/v1/skills/{id}/stats          → {usage_count, helpful_count, usefulness_score}
PUT  /api/v1/skills/{id}                → update suppressed/importance_score
```

Already implemented (stable):
```
POST /api/v1/skills/profile              ✓
GET  /api/v1/skills/pack                 ✓ (Phase 1 only, no pack_id yet)
POST /api/v1/skills/generate-for-domain  ✓
POST /api/v1/skills/publish              ✓
```

---

## 9. Metrics & Success Criteria

| Metric | Target | How measured |
|--------|--------|--------------|
| Pack relevance | ≥ 70% of delivered skills are referenced | outcome tracker |
| Cold-start coverage | < 3 prompts before domain has a skill | gap detector |
| Skill suppression precision | < 5% false-positive suppressions | manual review sample |
| Hook latency P95 | < 500ms (Phase 1 only) | hook timing log |
| Usefulness score stability | < 10% variance after 20+ uses | evolver report |

---

## 10. Rollout Plan

| Phase | What | When |
|-------|------|------|
| **v1 (done)** | profile + pack + generate-for-domain + hook | ✓ 2026-03-15 |
| **v2** | `POST /skills/pack/create` with pack_id; `POST /skills/outcome` | next sprint |
| **v3** | Skill Evolver cron; usefulness_score in selection | after 2 weeks of outcome data |
| **v4** | Dialogue Analyzer (auto outcome from transcript) | after v3 stable |
| **v5** | Phase 2 async enrichment with re-injection | after v4 stable |

---

## 11. Open Questions

1. **Hook re-injection**: Can `UserPromptSubmit` hook be called twice per prompt (immediate + enriched)? Needs Claude Code hook docs verification.
2. **Outcome attribution**: How to reliably detect "agent used skill X" from transcript without ground truth labels?
3. **Cross-agent skills**: Should usefulness scores be per-agent or global? Global risks noise from different task types.
4. **Suppression recovery**: If a skill is suppressed but the domain becomes active again, how to resurface it?
