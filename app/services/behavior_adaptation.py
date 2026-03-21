"""
Behavioral adaptation helpers (conditional reflexes).

This module contains the policy for when a repeated low-risk action becomes
"automatable" (suggest_automation=True).

It is deliberately separate from FastAPI routers so it can be reused across:
  - /skills/behavior/* endpoints
  - internal server-side hooks (e.g. auto-recording habits)
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.adaptive_state import get_adaptive_store


HIGH_RISK_ACTIONS: frozenset[str] = frozenset(
    {
        "delete_file",
        "drop_table",
        "force_push",
        "rm_rf",
        "send_email",
        "post_to_slack",
        "deploy_production",
        "overwrite_config",
        "revoke_permissions",
    }
)

SUGGEST_MIN_ACCEPTS = 5
SUGGEST_CONFIDENCE = 0.85
RECENT_WINDOW = 10  # last N events for decay calculation


def behavior_confidence(accepts: int, rejects: int) -> float:
    """Laplace-smoothed confidence from accept/reject history."""
    return round(accepts / (accepts + rejects + 1), 4)


def behavior_recent_confidence(recent: list[bool]) -> float:
    """Confidence over the most recent window (for decay detection)."""
    if not recent:
        return 0.0
    return round(sum(recent) / len(recent), 4)


def should_suggest_automation(
    *,
    action_type: str,
    accepts: int,
    rejects: int,
    recent: list[bool],
) -> bool:
    if action_type in HIGH_RISK_ACTIONS:
        return False
    conf = behavior_confidence(accepts, rejects)
    recent_conf = behavior_recent_confidence(recent)
    return (
        accepts >= SUGGEST_MIN_ACCEPTS
        and conf >= SUGGEST_CONFIDENCE
        and recent_conf >= SUGGEST_CONFIDENCE
    )


@dataclass(frozen=True)
class BehaviorEval:
    action_type: str
    context_signature: str
    accepts: int
    rejects: int
    confidence: float
    recent_confidence: float
    suggest_automation: bool
    high_risk: bool


def record_behavior_event(
    *,
    agent_id: str,
    action_type: str,
    accepted: bool,
    context_signature: str = "",
    recent_window: int = RECENT_WINDOW,
) -> BehaviorEval:
    entry = get_adaptive_store().record_behavior(
        agent_id=agent_id,
        action_type=action_type,
        accepted=accepted,
        context_sig=context_signature,
        recent_window=recent_window,
    )
    conf = behavior_confidence(entry["accepts"], entry["rejects"])
    recent_conf = behavior_recent_confidence(entry["recent"])
    high_risk = action_type in HIGH_RISK_ACTIONS
    suggest = should_suggest_automation(
        action_type=action_type,
        accepts=entry["accepts"],
        rejects=entry["rejects"],
        recent=entry["recent"],
    )
    return BehaviorEval(
        action_type=action_type,
        context_signature=context_signature,
        accepts=entry["accepts"],
        rejects=entry["rejects"],
        confidence=conf,
        recent_confidence=recent_conf,
        suggest_automation=suggest,
        high_risk=high_risk,
    )


def iter_behavior_evals(agent_id: str) -> list[BehaviorEval]:
    rows = get_adaptive_store().list_patterns(agent_id)
    evals: list[BehaviorEval] = []
    for entry in rows:
        action_type = entry["action_type"]
        context_sig = entry["context_sig"]
        accepts = int(entry["accepts"])
        rejects = int(entry["rejects"])
        recent = entry["recent"]
        evals.append(
            BehaviorEval(
                action_type=action_type,
                context_signature=context_sig,
                accepts=accepts,
                rejects=rejects,
                confidence=behavior_confidence(accepts, rejects),
                recent_confidence=behavior_recent_confidence(recent),
                suggest_automation=should_suggest_automation(
                    action_type=action_type,
                    accepts=accepts,
                    rejects=rejects,
                    recent=recent,
                ),
                high_risk=action_type in HIGH_RISK_ACTIONS,
            )
        )
    return evals

