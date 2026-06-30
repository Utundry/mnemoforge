from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.routers import mcp_sse


SPEC_PATH = Path(__file__).resolve().parents[1] / "app" / "mcp_specs" / "e2e" / "weak_model_scenarios.json"


def _load_spec() -> dict:
    return json.loads(SPEC_PATH.read_text(encoding="utf-8"))


def _scenario_by_id(scenario_id: str) -> dict:
    return next(item for item in _load_spec()["scenarios"] if item["id"] == scenario_id)


async def _call_public_tool(step: dict) -> dict:
    return json.loads(await mcp_sse._execute_tool(step["tool"], deepcopy(step["arguments"]), "http://test"))


def _fill_tokens(value, tokens: dict[str, str]):
    if isinstance(value, dict):
        return {key: _fill_tokens(item, tokens) for key, item in value.items()}
    if isinstance(value, list):
        return [_fill_tokens(item, tokens) for item in value]
    if isinstance(value, str) and value.startswith("$"):
        return tokens[value[1:]]
    return value


def test_weak_model_scenario_spec_is_ascii_and_public_surface_only() -> None:
    raw = SPEC_PATH.read_text(encoding="utf-8")
    assert raw.isascii()
    forbidden_shell_fragments = ("./", ".\\", "pytest", "docker", "powershell", "cmd.exe")
    assert not any(fragment in raw.casefold() for fragment in forbidden_shell_fragments)
    spec = _load_spec()
    assert spec["runtime_profile_id"] == "weak_mcp_operator"
    for scenario in spec["scenarios"]:
        tools = [scenario["tool"]] if "tool" in scenario else [step["tool"] for step in scenario["steps"]]
        assert set(tools) <= {"help", "state", "get", "submit"}


@pytest.mark.asyncio
async def test_weak_model_planning_state_is_short_and_actionable() -> None:
    scenario = _scenario_by_id("planning_state_is_short")
    data = await _call_public_tool(scenario)
    expect = scenario["expect"]

    form_ids = [form["form_id"] for form in data["forms"]]
    assert len(form_ids) <= expect["max_forms"]
    assert set(expect["required_forms"]) <= set(form_ids)
    assert set(expect["omitted_forms"]) <= set(data["omitted_forms"])
    for form in data["forms"]:
        for field in expect["forbidden_form_fields"]:
            assert field not in form
    assert data["simple_interface"]["tools"] == ["help", "state", "get", "submit"]


@pytest.mark.asyncio
async def test_weak_model_project_query_returns_items_not_json_blob(monkeypatch) -> None:
    scenario = _scenario_by_id("project_query_returns_items_not_json_blob")

    async def fake_ask_project(api_base: str, args: dict, *, session_id: str | None = None):
        return {
            "facade": "ask_project",
            "question": args["question"],
            "selected_expert_route": {"facade": "project_work"},
            "result_text": json.dumps(
                {
                    "status": "executed",
                    "action_status": "executed",
                    "selected_route": {
                        "tool": "list_open_tasks",
                        "family": "project_knowledge",
                        "intent_type": "list_all_tasks",
                        "confidence": 0.9,
                    },
                    "compact_result": [
                        {"task_id": "task-1", "title": "First weak-model task", "status": "open"}
                    ],
                    "route_telemetry": {"debug": "hidden"},
                }
            ),
        }

    monkeypatch.setattr(mcp_sse, "_build_ask_project_payload", fake_ask_project)
    data = await _call_public_tool(scenario)
    result = data["result"]
    expect = scenario["expect"]

    assert result["selected_facade"] == expect["selected_facade"]
    assert result["selected_route"]["tool"] == expect["selected_tool"]
    assert len(result["items"]) >= expect["min_items"]
    for field in expect["forbidden_result_fields"]:
        assert field not in result


@pytest.mark.asyncio
async def test_weak_model_memory_query_uses_memory_search(monkeypatch) -> None:
    scenario = _scenario_by_id("memory_query_uses_memory_search")

    async def fake_post(api_base: str, path: str, payload: dict):
        assert path == "/memories/search"
        return [
            {
                "score": 0.93,
                "memory": {
                    "id": "memory-1",
                    "content": "Weak model usability note.",
                    "memory_type": "context",
                    "category": "mnemoforge:fact",
                    "project": payload["project"],
                },
            }
        ]

    async def forbidden_ask_project(api_base: str, args: dict, *, session_id: str | None = None):
        raise AssertionError("memory scenario must not call project expert")

    monkeypatch.setattr(mcp_sse, "_post", fake_post)
    monkeypatch.setattr(mcp_sse, "_build_ask_project_payload", forbidden_ask_project)

    data = await _call_public_tool(scenario)
    expect = scenario["expect"]

    assert data["receipt"]["resource_kind"] == expect["resource_kind"]
    assert data["simple_interface"]["route"] == expect["route"]
    assert len(data["result"]) >= expect["min_items"]


@pytest.mark.asyncio
async def test_weak_model_public_submit_task_lifecycle(monkeypatch) -> None:
    from app.services import stenographer_service as stenographer_mod
    from app.services import task_lease_service as lease_mod

    scenario = _scenario_by_id("task_lifecycle_public_submit")
    lease_store = lease_mod.TaskLeaseStore(Path(":memory:"))
    stenographer_store = stenographer_mod.StenographerStore(Path(":memory:"))
    monkeypatch.setattr(lease_mod, "_STORE", lease_store)
    monkeypatch.setattr(stenographer_mod, "_STORE", stenographer_store)
    monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

    async def fake_post(api_base: str, path: str, payload: dict):
        if path.startswith("/project/tasks/") and path.endswith("/changes"):
            return {"id": f"checkpoint-{payload.get('stage') or payload.get('change_type')}", **payload}
        raise AssertionError(path)

    monkeypatch.setattr(mcp_sse, "_post", fake_post)
    tokens: dict[str, str] = {}
    try:
        for step in scenario["steps"]:
            filled = deepcopy(step)
            filled["arguments"] = _fill_tokens(filled["arguments"], tokens)
            data = await _call_public_tool(filled)
            receipt = data["receipt"]
            expect = step["expect"]
            assert receipt["status"] == expect["receipt_status"]
            for field in expect.get("receipt_fields", []):
                assert receipt.get(field)
            if receipt.get("work_token"):
                tokens["work_token"] = receipt["work_token"]
            if "release_status" in expect:
                assert receipt["release"]["status"] == expect["release_status"]
        assert lease_store.get_active_claim(project="alpha", task_id="fixture-public-mcp-lifecycle") is None
    finally:
        lease_store.close()
        stenographer_store.close()


@pytest.mark.asyncio
async def test_weak_model_cold_start_query_uses_project_readiness(monkeypatch) -> None:
    scenario = _scenario_by_id("cold_start_query_uses_project_readiness")

    async def fake_post(api_base: str, path: str, payload: dict):
        assert path == "/project/readiness"
        assert payload == {"project_id": "alpha"}
        return {
            "project_id": "alpha",
            "readiness_level": "bootstrap_needed",
            "readiness_score": 0.2,
            "summary": "Project memory needs initialization.",
            "blocking_gaps": ["No project overview"],
            "recommended_actions": ["Create a project overview context page"],
        }

    async def forbidden_ask_project(api_base: str, args: dict, *, session_id: str | None = None):
        raise AssertionError("cold-start scenario must not call project expert")

    monkeypatch.setattr(mcp_sse, "_post", fake_post)
    monkeypatch.setattr(mcp_sse, "_build_ask_project_payload", forbidden_ask_project)

    data = await _call_public_tool(scenario)
    expect = scenario["expect"]

    assert data["receipt"]["resource_kind"] == expect["resource_kind"]
    assert data["receipt"]["route"] == expect["route"]
    assert data["result"]["project_id"] == expect["project_id"]
    assert data["result"]["readiness_level"] == "bootstrap_needed"

