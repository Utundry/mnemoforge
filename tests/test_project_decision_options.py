import pytest


PREFIX = "/api/v1"


@pytest.mark.asyncio
async def test_rank_decision_options_persists_project_scoped_plan(client):
    response = await client.post(
        f"{PREFIX}/project/decision-options/rank",
        json={
            "project_id": "decision-alpha",
            "task": "Choose the next execution step",
            "agent_id": "codex",
            "options": [
                {
                    "label": "Implement durable packet metadata now",
                    "description": "Closes production reliability gap.",
                    "impact_score": 0.95,
                    "confidence_score": 0.8,
                    "urgency_score": 0.9,
                    "effort_score": 0.3,
                    "risk_score": 0.2,
                    "tags": ["reliability"],
                },
                {
                    "label": "Run optional cleanup pass",
                    "description": "Helpful but not critical.",
                    "impact_score": 0.5,
                    "confidence_score": 0.7,
                    "urgency_score": 0.4,
                    "effort_score": 0.4,
                    "risk_score": 0.2,
                    "tags": ["cleanup"],
                },
            ],
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["project_id"] == "decision-alpha"
    assert data["artifact_id"]
    assert len(data["ranked_options"]) == 2
    assert data["ranked_options"][0]["label"] == "Implement durable packet metadata now"
    assert data["ranked_options"][0]["rank"] == 1
    assert float(data["ranked_options"][0]["score"]) >= float(data["ranked_options"][1]["score"])

    listed = await client.get(
        f"{PREFIX}/project/decision-options",
        params={"project_id": "decision-alpha", "limit": 5, "include_ranked_options": True},
    )
    assert listed.status_code == 200, listed.text
    list_data = listed.json()
    assert list_data["found"] >= 1
    assert list_data["items"][0]["project_id"] == "decision-alpha"
    assert list_data["items"][0]["top_option"]["label"] == "Implement durable packet metadata now"
    assert len(list_data["items"][0]["ranked_options"]) == 2


@pytest.mark.asyncio
async def test_list_decision_options_filters_by_project(client):
    first = await client.post(
        f"{PREFIX}/project/decision-options/rank",
        json={
            "project_id": "decision-filter-a",
            "task": "Project A options",
            "options": [
                {
                    "label": "A1",
                    "impact_score": 0.8,
                    "confidence_score": 0.8,
                    "urgency_score": 0.6,
                    "effort_score": 0.3,
                    "risk_score": 0.2,
                }
            ],
        },
    )
    second = await client.post(
        f"{PREFIX}/project/decision-options/rank",
        json={
            "project_id": "decision-filter-b",
            "task": "Project B options",
            "options": [
                {
                    "label": "B1",
                    "impact_score": 0.9,
                    "confidence_score": 0.8,
                    "urgency_score": 0.7,
                    "effort_score": 0.4,
                    "risk_score": 0.3,
                }
            ],
        },
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text

    listed = await client.get(
        f"{PREFIX}/project/decision-options",
        params={"project_id": "decision-filter-a", "limit": 10},
    )
    assert listed.status_code == 200, listed.text
    data = listed.json()
    assert data["found"] >= 1
    assert all(item["project_id"] == "decision-filter-a" for item in data["items"])
    assert all("Project B options" != item["task"] for item in data["items"])


@pytest.mark.asyncio
async def test_deferred_findings_are_recorded_and_listed_by_project(client):
    created = await client.post(
        f"{PREFIX}/project/deferred-findings",
        json={
            "project_id": "deferred-alpha",
            "finding": "Observed flaky integration test in optional path.",
            "suggested_follow_up": "Re-run under CI load and capture failing seed.",
            "why_it_matters": "Can hide regressions in post-merge checks.",
            "severity": "medium",
            "agent_id": "codex",
            "tags": ["tests", "postprocessing"],
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["artifact_id"]
    assert body["project_id"] == "deferred-alpha"
    assert body["severity"] == "medium"

    listed = await client.get(
        f"{PREFIX}/project/deferred-findings",
        params={"project_id": "deferred-alpha", "limit": 10},
    )
    assert listed.status_code == 200, listed.text
    data = listed.json()
    assert data["found"] >= 1
    assert any(
        item["finding"] == "Observed flaky integration test in optional path."
        and item["severity"] == "medium"
        for item in data["items"]
    )


@pytest.mark.asyncio
async def test_deferred_findings_can_be_filtered_by_task_id(client):
    first = await client.post(
        f"{PREFIX}/project/deferred-findings",
        json={
            "project_id": "deferred-task-alpha",
            "task_id": "task-a",
            "finding": "Task A follow-up",
            "suggested_follow_up": "Do A later",
            "severity": "low",
        },
    )
    second = await client.post(
        f"{PREFIX}/project/deferred-findings",
        json={
            "project_id": "deferred-task-alpha",
            "task_id": "task-b",
            "finding": "Task B follow-up",
            "suggested_follow_up": "Do B later",
            "severity": "medium",
        },
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text

    listed = await client.get(
        f"{PREFIX}/project/deferred-findings",
        params={"project_id": "deferred-task-alpha", "task_id": "task-a", "limit": 10},
    )
    assert listed.status_code == 200, listed.text
    data = listed.json()
    assert data["task_id"] == "task-a"
    assert data["found"] >= 1
    assert all(item["task_id"] == "task-a" for item in data["items"])
    assert any(item["finding"] == "Task A follow-up" for item in data["items"])


@pytest.mark.asyncio
async def test_audit_findings_can_be_created_and_listed(client):
    created = await client.post(
        f"{PREFIX}/project/audit-findings",
        json={
            "project_id": "audit-alpha",
            "title": "Missing source attribution in generated docs",
            "details": "Docs projection merged external text without source marker.",
            "finding_source": "manual_review",
            "finding_type": "docs_governance",
            "severity": "high",
            "agent_id": "codex",
            "tags": ["docs", "governance"],
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["artifact_id"]
    assert body["project_id"] == "audit-alpha"
    assert body["severity"] == "high"

    listed = await client.get(
        f"{PREFIX}/project/audit-findings",
        params={"project_id": "audit-alpha", "limit": 10},
    )
    assert listed.status_code == 200, listed.text
    data = listed.json()
    assert data["found"] >= 1
    assert any(
        item["title"] == "Missing source attribution in generated docs"
        and item["finding_source"] == "manual_review"
        for item in data["items"]
    )


@pytest.mark.asyncio
async def test_auto_capture_audit_findings_from_integrity_store(client, monkeypatch):
    class _FakeIntegrityStore:
        def list_findings(self, status: str = "", limit: int = 20):
            assert status == "suspect"
            return [
                {
                    "finding_id": "slice-a:row-1:missing_meta",
                    "slice_id": "slice-a",
                    "category": "handoff",
                    "suspicion_type": "missing_meta",
                    "confidence": 0.9,
                    "details": "meta payload is empty for this handoff record",
                }
            ]

    monkeypatch.setattr("app.routers.project.get_data_integrity_store", lambda: _FakeIntegrityStore())

    captured = await client.post(
        f"{PREFIX}/project/audit-findings/auto-capture",
        json={"project_id": "audit-capture-alpha", "source": "integrity", "limit": 5, "agent_id": "codex"},
    )
    assert captured.status_code == 200, captured.text
    data = captured.json()
    assert data["project_id"] == "audit-capture-alpha"
    assert data["source"] == "integrity"
    assert data["captured"] == 1
    assert data["items"][0]["source_finding_id"] == "slice-a:row-1:missing_meta"

    listed = await client.get(
        f"{PREFIX}/project/audit-findings",
        params={"project_id": "audit-capture-alpha", "limit": 10},
    )
    assert listed.status_code == 200, listed.text
    list_data = listed.json()
    assert list_data["found"] >= 1
    assert any(item["finding_source"] == "data_integrity" for item in list_data["items"])
