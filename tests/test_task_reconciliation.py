from __future__ import annotations

import json

import pytest

from app.services.planning_advisor_service import build_next_work_advisor
from app.services.task_reconciliation_service import get_task_reconciliation_store


def test_task_reconciliation_packet_and_planning_advisor_demotes_covered_task():
    store = get_task_reconciliation_store()
    target = "task:alpha:old-parent"
    implemented = "task:alpha:new-impl"
    record = store.record_decision(
        target_task_ref=target,
        implemented_task_ref=implemented,
        decision="supersede",
        reason="Generic implementation covered this refinement.",
        acted_by="tester",
        evidence_refs=["context_page:evidence"],
    )
    assert record["decision"] == "supersede"
    packet = store.packet_for_target(target)
    assert packet["covered_by_implementation"] is True
    assert packet["implemented_task_ref"] == implemented

    advisor = build_next_work_advisor(
        {
            "items": [
                {"artifact_key": target, "type": "task", "task_id": "old-parent", "project": "alpha", "title": "Old parent", "status": "open"},
                {"artifact_key": "task:alpha:next", "type": "task", "task_id": "next", "project": "alpha", "title": "Next task", "status": "open"},
            ]
        },
        project="alpha",
        query="what next",
        limit=5,
    )
    refs = [item["ref"] for item in advisor["next_work_candidates"]]
    assert refs == ["task:alpha:next", target]
    covered = advisor["next_work_candidates"][1]
    assert covered["reconciliation"]["decision"] == "supersede"
    assert "reconciliation_warning" in advisor


@pytest.mark.asyncio
async def test_task_reconciliation_api_and_mcp_form(client):
    resp = await client.post(
        "/api/v1/task-reconciliation/review",
        json={
            "target_task_ref": "task:alpha:old",
            "implemented_task_ref": "task:alpha:new",
            "decision": "link",
            "reason": "Covered by implementation.",
            "acted_by": "tester",
        },
    )
    assert resp.status_code == 200, resp.text
    packet = resp.json()["packet"]
    assert packet["decision"] == "link"
    assert packet["covered_by_implementation"] is True

    read = await client.get("/api/v1/task-reconciliation/packet", params={"target_task_ref": "task:alpha:old"})
    assert read.status_code == 200
    assert read.json()["implemented_task_ref"] == "task:alpha:new"


@pytest.mark.asyncio
async def test_mcp_review_task_reconciliation_and_read_packet(monkeypatch):
    from app.routers import mcp_sse

    async def fake_post(api_base: str, path: str, payload: dict):
        assert path == "/task-reconciliation/review"
        decision = get_task_reconciliation_store().record_decision(**payload)
        return {
            "decision_record": decision,
            "packet": get_task_reconciliation_store().packet_for_target(payload["target_task_ref"]),
            "source_of_truth": "sqlite",
        }

    monkeypatch.setattr(mcp_sse, "_post", fake_post)
    result = json.loads(
        await mcp_sse._execute_tool(
            "submit",
            {
                "project": "alpha",
                "state": "operator_review",
                "form_id": "review_task_reconciliation",
                "payload": {
                    "project": "alpha",
                    "target_task_ref": "task:alpha:old",
                    "implemented_task_ref": "task:alpha:new",
                    "decision": "supersede",
                    "reason": "Covered by implementation.",
                    "acted_by": "tester",
                },
            },
            "http://test/api/v1",
        )
    )
    assert result["receipt"]["status"] == "accepted"
    assert result["receipt"]["decision"] == "supersede"

    packet = json.loads(
        await mcp_sse._execute_tool(
            "get",
            {"project": "alpha", "query": "task reconciliation packet for task:alpha:old", "response_format": "json"},
            "http://test/api/v1",
        )
    )
    assert packet["receipt"]["resource_kind"] == "task_reconciliation"
    assert packet["result"]["covered_by_implementation"] is True
