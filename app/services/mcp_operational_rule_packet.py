"""Operational rule packet selection helpers for MCP responses."""
from __future__ import annotations

import re
from typing import Any


def rule_packet_tokens(*parts: str) -> set[str]:
    tokens: set[str] = set()
    for part in parts:
        for token in re.findall(r"[\w]+", str(part or "").casefold(), flags=re.UNICODE):
            if len(token) >= 3 or (len(token) >= 2 and any(ord(char) > 127 for char in token)):
                tokens.add(token)
    return tokens


def rule_packet_score(rule: dict[str, Any], *, query_tokens: set[str], required_bonus: float) -> float:
    hay = rule_packet_tokens(
        str(rule.get("id") or ""),
        str(rule.get("title") or ""),
        str(rule.get("reason") or ""),
        str(rule.get("topic_path") or ""),
        str(rule.get("statement") or ""),
        " ".join(str(x) for x in (rule.get("tags") or [])),
    )
    if not query_tokens:
        base = 0.0
    else:
        base = len(query_tokens.intersection(hay)) / max(1.0, float(len(query_tokens)))
    if str(rule.get("_rule_source") or "") == "required":
        base += required_bonus
    return min(1.0, max(0.0, base))


def build_operational_rule_packet(context: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    detail = str(args.get("detail") or "compact").strip().lower()
    if detail not in {"compact", "full"}:
        detail = "compact"
    threshold = float(args.get("relevance_threshold", 0.25) or 0.25)
    threshold = max(0.0, min(1.0, threshold))
    top_limit = max(1, min(10, int(args.get("top_rules_limit", 3) or 3)))
    available_limit = max(1, min(30, int(args.get("available_rules_limit", 12) or 12)))
    required_bonus = 0.2

    required = context.get("required_rules") if isinstance(context.get("required_rules"), list) else []
    recommended = context.get("recommended_rules") if isinstance(context.get("recommended_rules"), list) else []
    combined: list[dict[str, Any]] = []
    for item in required:
        if isinstance(item, dict):
            merged = dict(item)
            merged["_rule_source"] = "required"
            combined.append(merged)
    for item in recommended:
        if isinstance(item, dict):
            merged = dict(item)
            merged["_rule_source"] = "recommended"
            combined.append(merged)

    intent_tokens = rule_packet_tokens(
        str(args.get("task") or ""),
        str(args.get("intent") or ""),
        str(args.get("state") or ""),
        " ".join(str(x) for x in (args.get("changed_files") or [])),
    )
    scored: list[tuple[float, dict[str, Any]]] = []
    for rule in combined:
        score = rule_packet_score(rule, query_tokens=intent_tokens, required_bonus=required_bonus)
        if score >= threshold:
            scored.append((score, rule))
    scored.sort(key=lambda row: row[0], reverse=True)
    if not scored and combined:
        scored = [(0.0, rule) for rule in combined]

    top_rows = scored[:top_limit]
    top_ids = {str(row[1].get("id") or row[1].get("title") or "") for row in top_rows}
    applied_rules: list[dict[str, Any]] = []
    for score, rule in top_rows:
        clean_rule = {k: v for k, v in rule.items() if not str(k).startswith("_")}
        clean_rule["relevance_score"] = round(score, 4)
        clean_rule["rule_source"] = str(rule.get("_rule_source") or "")
        applied_rules.append(clean_rule)

    available_rows = [(score, rule) for score, rule in scored if str(rule.get("id") or rule.get("title") or "") not in top_ids]
    available_rules: list[dict[str, Any]] = []
    for score, rule in available_rows[:available_limit]:
        available_rules.append(
            {
                "rule_id": str(rule.get("id") or rule.get("title") or ""),
                "title": str(rule.get("title") or rule.get("id") or "rule"),
                "rule_source": str(rule.get("_rule_source") or ""),
                "relevance_score": round(score, 4),
                "why_matched": str(rule.get("reason") or ""),
            }
        )

    selected_rule_id = str(args.get("rule_id") or "").strip()
    selected_rule = None
    if selected_rule_id:
        for _, rule in scored:
            rid = str(rule.get("id") or rule.get("title") or "").strip()
            if rid == selected_rule_id:
                selected_rule = {k: v for k, v in rule.items() if not str(k).startswith("_")}
                break

    packet: dict[str, Any] = {
        "detail": detail,
        "relevance_threshold": threshold,
        "applied_rules": applied_rules,
        "available_rules": available_rules,
        "available_count": len(available_rows),
        "pull_hint": "Call operational_tray action=inspect with rule_id to fetch one rule in full form.",
    }
    if detail == "full":
        packet["available_rules_full"] = [
            {
                **{k: v for k, v in rule.items() if not str(k).startswith("_")},
                "relevance_score": round(score, 4),
                "rule_source": str(rule.get("_rule_source") or ""),
            }
            for score, rule in available_rows[:available_limit]
        ]
    if selected_rule is not None:
        packet["selected_rule"] = selected_rule
    return packet
