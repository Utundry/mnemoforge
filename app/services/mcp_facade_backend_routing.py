"""Shared backend selection for MCP facade route selection."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

RouteFn = Callable[..., dict[str, Any]]
InvalidateLearnedCallback = Callable[..., dict[str, Any] | None]
LearnedRouteCallback = Callable[..., dict[str, Any] | None]
RouteNeedsLlmCallback = Callable[[list[dict[str, Any]]], bool]
LlmDisambiguateCallback = Callable[..., Awaitable[dict[str, Any]]]
RecordLearnedCallback = Callable[..., str]
FormatErrorCallback = Callable[[Exception], str]


@dataclass(frozen=True)
class FacadeBackendRoutingDependencies:
    invalidate_conflicting_learned_route: InvalidateLearnedCallback
    learned_route_match: LearnedRouteCallback
    route_needs_llm_disambiguation: RouteNeedsLlmCallback
    llm_disambiguate: LlmDisambiguateCallback
    record_learned_route_pattern: RecordLearnedCallback
    format_error: FormatErrorCallback


def facade_backend_requested(args: dict[str, Any]) -> str:
    backend_requested = str(args.get("scorer_backend") or "auto").strip().lower() or "auto"
    return backend_requested if backend_requested in {"lexical", "auto", "llm"} else "auto"


def facade_text(args: dict[str, Any]) -> str:
    return " ".join(
        part
        for part in (
            str(args.get("intent") or "").strip(),
            str(args.get("question") or "").strip(),
            str(args.get("query") or "").strip(),
            str(args.get("task") or "").strip(),
            str(args.get("summary") or "").strip(),
            str(args.get("raw_notes") or "").strip(),
            str(args.get("state") or "").strip(),
        )
        if part
    )


async def facade_route_with_backend(
    *,
    facade: str,
    args: dict[str, Any],
    catalog: tuple[dict[str, Any], ...],
    route_fn: RouteFn,
    dependencies: FacadeBackendRoutingDependencies,
) -> dict[str, Any]:
    backend_requested = facade_backend_requested(args)
    lexical_route = route_fn(
        args,
        scorer_meta={
            "backend_requested": backend_requested,
            "backend_used": "lexical",
            "llm_attempted": False,
            "fallback_reason": "",
        },
    )
    candidates = lexical_route.get("route_candidates") or []
    allowed_intent_types = {str(route["intent_type"]) for route in catalog}
    if lexical_route.get("structural_match"):
        invalidated = dependencies.invalidate_conflicting_learned_route(
            facade=facade,
            args=args,
            structural_route=lexical_route,
            allowed_intent_types=allowed_intent_types,
        )
        if invalidated:
            scorer = lexical_route.get("scorer") if isinstance(lexical_route.get("scorer"), dict) else {}
            lexical_route["scorer"] = {
                **scorer,
                "invalidated_learned_pattern_id": invalidated.get("pattern_id", ""),
                "invalidated_learned_pattern": invalidated,
            }
        return lexical_route

    should_try_llm = backend_requested == "llm" or (
        backend_requested == "auto" and dependencies.route_needs_llm_disambiguation(candidates)
    )
    if not should_try_llm:
        return lexical_route

    text = facade_text(args)
    if backend_requested == "auto":
        learned = dependencies.learned_route_match(
            facade=facade,
            text=text,
            allowed_intent_types=allowed_intent_types,
        )
        if learned:
            return route_fn(
                args,
                llm_decision=learned,
                scorer_meta={
                    "backend_requested": backend_requested,
                    "backend_used": learned.get("backend_used") or "learned_semantic",
                    "llm_attempted": False,
                    "fallback_reason": "",
                    "matched_pattern_id": learned.get("pattern_id") or "",
                    "matched_pattern_score": learned.get("score"),
                    "matched_by": learned.get("matched_by") or "",
                },
            )
    try:
        decision = await dependencies.llm_disambiguate(
            facade=facade,
            text=text,
            args=args,
            candidates=candidates,
            catalog=catalog,
        )
    except Exception as exc:
        lexical_route["scorer"] = {
            "backend_requested": backend_requested,
            "backend_used": "lexical",
            "llm_attempted": True,
            "fallback_reason": dependencies.format_error(exc),
        }
        return lexical_route

    if not decision:
        lexical_route["scorer"] = {
            "backend_requested": backend_requested,
            "backend_used": "lexical",
            "llm_attempted": True,
            "fallback_reason": f"LLM returned no valid {facade} intent_type.",
        }
        return lexical_route

    route = route_fn(
        args,
        llm_decision=decision,
        scorer_meta={
            "backend_requested": backend_requested,
            "backend_used": "llm",
            "llm_attempted": True,
            "fallback_reason": "",
            "llm_reason": str(decision.get("reason") or "").strip(),
        },
    )
    pattern_id = dependencies.record_learned_route_pattern(facade=facade, text=text, route=route, decision=decision, args=args)
    if pattern_id:
        route.setdefault("scorer", {})["learned_pattern_id"] = pattern_id
    return route
