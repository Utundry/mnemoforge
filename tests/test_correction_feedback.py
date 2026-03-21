"""
Tests for specialist feedback / task_type correction signal.

Analogy: Ivanov's feedback — "this isn't my task, send it to Sidorov next time."
  - POST /tracker/record with corrected_task_type stores the correction signal
  - GET /tracker/corrections aggregates: when classified=X, actual=Y, how often
  - POST /router/decide includes correction_hints when routing similar tasks
"""
from __future__ import annotations

import pytest


# ── Feature 1: Recording corrections ─────────────────────────────────────────


class TestRecordCorrection:
    @pytest.mark.asyncio
    async def test_record_with_corrected_task_type(self, client):
        """POST /tracker/record accepts corrected_task_type and returns it."""
        resp = await client.post("/api/v1/tracker/record", json={
            "component": "cloud-llm",
            "task_type": "text_summarization",
            "success": True,
            "corrected_task_type": "code_review",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["corrected_task_type"] == "code_review"
        assert "correction_note" in data
        assert "code_review" in data["correction_note"]

    @pytest.mark.asyncio
    async def test_record_without_correction_no_note(self, client):
        """POST /tracker/record without corrected_task_type returns no correction_note."""
        resp = await client.post("/api/v1/tracker/record", json={
            "component": "cloud-llm",
            "task_type": "code_generation",
            "success": True,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "corrected_task_type" not in data
        assert "correction_note" not in data

    @pytest.mark.asyncio
    async def test_record_correction_event_id_returned(self, client):
        """Corrected records still return event_id."""
        resp = await client.post("/api/v1/tracker/record", json={
            "component": "qwen3:1.7b",
            "task_type": "layout_fix",
            "success": False,
            "corrected_task_type": "fact_extraction",
        })
        assert resp.status_code == 200
        assert "event_id" in resp.json()

    @pytest.mark.asyncio
    async def test_record_same_correction_does_not_appear_in_corrections(self, client):
        """If corrected_task_type equals task_type it should not appear as a correction."""
        # Store event where correction equals original (no-op correction)
        await client.post("/api/v1/tracker/record", json={
            "component": "cloud-llm",
            "task_type": "code_generation",
            "success": True,
            "corrected_task_type": "code_generation",  # same — not a real correction
        })
        resp = await client.get("/api/v1/tracker/corrections?task_type=code_generation")
        assert resp.status_code == 200
        # Should not appear (same type is filtered out in SQL)
        for row in resp.json():
            assert row["actual_type"] != row["classified_as"] or row["classified_as"] != "code_generation"


# ── Feature 2: GET /tracker/corrections ───────────────────────────────────────


class TestGetCorrections:
    @pytest.mark.asyncio
    async def test_corrections_empty_when_none_recorded(self, client):
        """GET /tracker/corrections returns empty list when no corrections exist."""
        resp = await client.get("/api/v1/tracker/corrections?task_type=nonexistent_task_xyz")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_corrections_aggregates_multiple_events(self, client):
        """Multiple corrections for same pair are aggregated into one row."""
        for _ in range(3):
            await client.post("/api/v1/tracker/record", json={
                "component": "cloud-llm",
                "task_type": "text_summarization",
                "success": True,
                "corrected_task_type": "code_review",
            })

        resp = await client.get("/api/v1/tracker/corrections?task_type=text_summarization&min_count=2")
        assert resp.status_code == 200
        rows = resp.json()
        matching = [r for r in rows if r["actual_type"] == "code_review"]
        assert len(matching) == 1
        assert matching[0]["count"] >= 3

    @pytest.mark.asyncio
    async def test_corrections_min_count_filter(self, client):
        """min_count=5 excludes pairs with fewer corrections."""
        await client.post("/api/v1/tracker/record", json={
            "component": "cloud-llm",
            "task_type": "query_expansion",
            "success": True,
            "corrected_task_type": "architecture",
        })

        resp = await client.get("/api/v1/tracker/corrections?task_type=query_expansion&min_count=5")
        assert resp.status_code == 200
        rows = resp.json()
        assert all(r["count"] >= 5 for r in rows)

    @pytest.mark.asyncio
    async def test_corrections_includes_correction_rate(self, client):
        """Each correction row includes correction_rate (0-1)."""
        await client.post("/api/v1/tracker/record", json={
            "component": "cloud-llm",
            "task_type": "skill_tagging",
            "success": True,
            "corrected_task_type": "relevance_scoring",
        })

        resp = await client.get("/api/v1/tracker/corrections?min_count=1")
        assert resp.status_code == 200
        for row in resp.json():
            assert 0.0 <= row["correction_rate"] <= 1.0

    @pytest.mark.asyncio
    async def test_corrections_response_shape(self, client):
        """Each row has classified_as, actual_type, count, correction_rate."""
        await client.post("/api/v1/tracker/record", json={
            "component": "cloud-llm",
            "task_type": "log_filter",
            "success": True,
            "corrected_task_type": "fact_extraction",
        })

        resp = await client.get("/api/v1/tracker/corrections?min_count=1")
        assert resp.status_code == 200
        for row in resp.json():
            assert "classified_as" in row
            assert "actual_type" in row
            assert "count" in row
            assert "correction_rate" in row

    @pytest.mark.asyncio
    async def test_corrections_sorted_by_count_desc(self, client):
        """Corrections are returned sorted by count descending."""
        # 3 corrections for pair A
        for _ in range(3):
            await client.post("/api/v1/tracker/record", json={
                "component": "cloud-llm",
                "task_type": "memory_extraction",
                "success": True,
                "corrected_task_type": "fact_extraction",
            })
        # 1 correction for pair B
        await client.post("/api/v1/tracker/record", json={
            "component": "cloud-llm",
            "task_type": "memory_extraction",
            "success": True,
            "corrected_task_type": "code_generation",
        })

        resp = await client.get("/api/v1/tracker/corrections?task_type=memory_extraction&min_count=1")
        assert resp.status_code == 200
        rows = resp.json()
        if len(rows) >= 2:
            assert rows[0]["count"] >= rows[1]["count"]


# ── Feature 3: correction_hints in /router/decide ────────────────────────────


class TestRoutingCorrectionHints:
    @pytest.mark.asyncio
    async def test_decide_response_has_correction_hints_field(self, client):
        """POST /router/decide always includes correction_hints field."""
        resp = await client.post("/api/v1/router/decide", json={
            "task": "Summarize this document",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "correction_hints" in data
        assert isinstance(data["correction_hints"], list)

    @pytest.mark.asyncio
    async def test_correction_hints_empty_with_no_corrections(self, client):
        """correction_hints is empty when no corrections recorded for this task_type."""
        resp = await client.post("/api/v1/router/decide", json={
            "task": "Check logs for errors",
            "task_type": "log_filter_unique_xyz",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["correction_hints"] == []

    @pytest.mark.asyncio
    async def test_correction_hints_populated_after_feedback(self, client):
        """correction_hints contains Ivanov's corrections after enough are recorded."""
        # Record 3 corrections: text_summarization → code_review
        for _ in range(3):
            await client.post("/api/v1/tracker/record", json={
                "component": "cloud-llm",
                "task_type": "text_summarization",
                "success": True,
                "corrected_task_type": "code_review",
            })

        resp = await client.post("/api/v1/router/decide", json={
            "task": "Summarize this code",
            "task_type": "text_summarization",
        })
        assert resp.status_code == 200
        data = resp.json()
        hints = data["correction_hints"]
        code_review_hints = [h for h in hints if h["actual_type"] == "code_review"]
        assert len(code_review_hints) >= 1
        assert code_review_hints[0]["count"] >= 3

    @pytest.mark.asyncio
    async def test_correction_hints_shape(self, client):
        """Each correction_hint has actual_type, count, correction_rate."""
        await client.post("/api/v1/tracker/record", json={
            "component": "cloud-llm",
            "task_type": "query_expansion",
            "success": True,
            "corrected_task_type": "architecture",
        })
        await client.post("/api/v1/tracker/record", json={
            "component": "cloud-llm",
            "task_type": "query_expansion",
            "success": True,
            "corrected_task_type": "architecture",
        })

        resp = await client.post("/api/v1/router/decide", json={
            "task": "Expand the query",
            "task_type": "query_expansion",
        })
        assert resp.status_code == 200
        for hint in resp.json()["correction_hints"]:
            assert "actual_type" in hint
            assert "count" in hint
            assert "correction_rate" in hint

    @pytest.mark.asyncio
    async def test_strong_correction_amends_reasoning(self, client):
        """When correction_rate >= 0.3, reasoning includes a warning note."""
        # Create many corrections to get high rate
        for _ in range(5):
            await client.post("/api/v1/tracker/record", json={
                "component": "cloud-llm",
                "task_type": "text_summarization",
                "success": True,
                "corrected_task_type": "architecture",
            })

        resp = await client.post("/api/v1/router/decide", json={
            "task": "Design the system architecture",
            "task_type": "text_summarization",
        })
        assert resp.status_code == 200
        data = resp.json()
        hints = [h for h in data["correction_hints"] if h["actual_type"] == "architecture"]
        if hints and hints[0]["correction_rate"] >= 0.3:
            assert "architecture" in data["reasoning"] or "corrected" in data["reasoning"].lower()
