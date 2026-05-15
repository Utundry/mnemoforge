# Task Continuity Semantic Audit

Date: 2026-05-15

Scope: semantic consistency for continue/resume, lease/release/ttl, and read-only versus mutating ownership gates.

## Canonical Model

| Intent | Canonical surface | Mutation | Ownership requirement |
| --- | --- | --- | --- |
| Continue current work from durable state | `pull_task_context` | No | Must report active occupancy if another session owns the lease |
| Start execution after replay | `start_task_session` | Yes | Creates or renews an owned session-scoped lease and work session |
| Keep execution lease alive | auto heartbeat or `heartbeat_task_claim` | Yes | Requires matching `owner_agent` and `session_id` |
| Finish execution | `finish_task_session` | Yes | Requires active owned lease, writes checkpoint, ends work session, releases lease |
| Reactivate a closed or inactive task | `reopen_task` | Yes | Explicit reactivation action; not the default for ordinary continuation |
| Release a standalone claim | `release_task_claim` | Yes | Requires matching `owner_agent` and `session_id` |
| Force-release stale ownership | `force_release_task_claim` | Yes | Requires explicit audit fields `acted_by` and `reason` |

## Discrepancy Matrix

| Area | Finding | Impact | Remediation |
| --- | --- | --- | --- |
| Resume terminology | Onboarding and tool recommendation previously said generic "resume task" should call `reopen_task`. | Agents could mutate task lifecycle when they only needed read-only checkpoint replay. | Fixed: generic continue/resume routes to `pull_task_context`; `reopen_task` is reserved for explicit reactivation. |
| Project facade | `project_work` already routed `continue task` and `resume from checkpoint` to `pull_task_context`. | This was the correct behavior, but contradicted lower-level guidance. | Preserved facade behavior and aligned normalization/recommendation with it. |
| Lease TTL | Session-scoped leases have TTL and auto-heartbeat in `start_task_session`. | Good current behavior after previous lease reliability work. | Keep test coverage for auto heartbeat and expired lease conflicts. |
| Finish lifecycle | `finish_task_session` requires owned active lease and releases it after closeout. | Good current behavior after previous guardrail work. | Keep closeout tests as the lifecycle regression gate. |
| Read-only replay occupancy | `pull_task_context` is read-only but reports `occupied` when another session has an active lease. | Good safety signal without mutating state. | Keep occupancy test coverage. |
| Direct reactivation gate | `reopen_task` remains a direct mutating lifecycle tool. | Requires operator or facade discipline; future hard gate may be needed if direct tools become common for weak clients. | Follow-up candidate: guarded direct lifecycle mutation mode for `reopen_task`/`reopen_artifact`. |

## Remediation Plan

1. Completed: align onboarding, `normalize_mcp_intent`, and `tool_recommend` so ordinary continuation starts with `pull_task_context`.
2. Completed: add regression tests for checkpoint resume versus explicit reactivation.
3. Keep existing regression gates for lease owner/session matching, TTL expiry, auto heartbeat, finish release, and read-only occupancy.
4. Future improvement: add an explicit direct-tool guardrail for lifecycle reactivation if weak clients keep bypassing `project_work`.

## Verification

Use the project Docker contour, not host pytest:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_pytest_docker.ps1 -NoBuild tests\test_mcp_sse.py tests\test_task_lease_service.py -q
```
