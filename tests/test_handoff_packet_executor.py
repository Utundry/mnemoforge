from __future__ import annotations

import pytest

from app.services.handoff_packet_executor import (
    build_handoff_packet_prompt,
    execute_handoff_packet,
)


def test_build_handoff_packet_prompt_includes_packet_context():
    system, prompt = build_handoff_packet_prompt(
        {
            "task_description": "Summarize recent API changes",
            "task_type": "text_summarization",
            "phase": "verification",
            "priority": "high",
            "why_now": "Operator needs a quick status note",
            "execution_mode": "economy",
            "recommended_executor": "background_llm",
            "recommendation_reason": "Read-only packet can run on external background model",
            "write_scope": ["docs/", "tests/"],
            "definition_of_done": "Produce a concise summary",
            "expected_output_shape": "Short operator note",
            "phase_objective": "Turn routing state into operator guidance",
            "core_instinct_ids": ["clarify_scope"],
            "supporting_instinct_ids": ["track_assumptions"],
            "routing_basis": {"component": "glm-4.7", "tier": "cloud", "reasoning": "Forced cloud tier."},
            "project_context_summary": "coverage laws=1, components=2",
            "project_context_refs": {"laws": ["law-1"], "components": ["router", "handoff"]},
            "project_context_snapshot": "## Relevant Components\n\n### Router\nRoutes bounded packets.",
        }
    )

    assert "bounded task-packet execution worker" in system
    assert "Task: Summarize recent API changes" in prompt
    assert "Task type: text_summarization" in prompt
    assert "Priority: high" in prompt
    assert "Why now: Operator needs a quick status note" in prompt
    assert "Routing basis:" in prompt
    assert "component: glm-4.7" in prompt
    assert "Project context summary:" in prompt
    assert "coverage laws=1, components=2" in prompt
    assert "laws: law-1" in prompt
    assert "Write scope: docs/, tests/" in prompt


@pytest.mark.asyncio
async def test_execute_handoff_packet_uses_selected_external_model_for_write_bearing_packet():
    class _FakeGateway:
        local_model = "qwen3:1.7b"

        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def generate(self, **kwargs):
            self.calls.append(kwargs)
            return """
{
  "summary": "Prepared a bounded implementation plan.",
  "deliverable": "Outlined the patch strategy for the packet.",
  "verification": "Main agent should review the proposed patch before applying it.",
  "implementation_plan": ["Update routing helper", "Add targeted tests"],
  "proposed_patch": [
    {"path": "app/routers/models.py", "change_type": "update", "summary": "Adjust packet routing metadata"}
  ]
}
"""

    gateway = _FakeGateway()
    result = await execute_handoff_packet(
        {
            "task_description": "Prepare a bounded patch proposal",
            "task_type": "code_generation",
            "execution_mode": "economy",
            "recommended_executor": "cheap_subagent",
            "recommended_model": "glm-4.7",
            "definition_of_done": "Produce a patch proposal",
            "expected_output_shape": "Short proposed patch summary",
            "write_scope": ["app/routers/models.py"],
        },
        gateway,
    )

    assert gateway.calls[0]["model_override"] == "glm-4.7"
    assert "Return one JSON object only" in gateway.calls[0]["prompt"]
    assert result["model_used"] == "glm-4.7"
    assert result["executor_used"] == "cheap_subagent"
    assert result["summary"] == "Prepared a bounded implementation plan."
    assert result["structured"] is True
    assert result["implementation_plan"] == ["Update routing helper", "Add targeted tests"]
    assert result["proposed_patch"][0]["path"] == "app/routers/models.py"


@pytest.mark.asyncio
async def test_execute_handoff_packet_keeps_text_output_for_read_only_packet():
    class _FakeGateway:
        local_model = "qwen3:1.7b"

        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def generate(self, **kwargs):
            self.calls.append(kwargs)
            return "Summary: Concise result\n\nDeliverable: Operator summary.\n\nVerification: None."

    gateway = _FakeGateway()
    result = await execute_handoff_packet(
        {
            "task_description": "Summarize routing behavior",
            "task_type": "text_summarization",
            "execution_mode": "economy",
            "recommended_executor": "background_llm",
            "recommended_model": "glm-4.7",
            "definition_of_done": "Produce a concise summary",
            "expected_output_shape": "Short operator note",
            "write_scope": [],
        },
        gateway,
    )

    assert "Return exactly these sections:" in gateway.calls[0]["prompt"]
    assert result["structured"] is False
    assert result["summary"].startswith("Summary:")


@pytest.mark.asyncio
async def test_execute_handoff_packet_normalizes_non_json_write_bearing_output():
    class _FakeGateway:
        local_model = "qwen3:1.7b"

        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def generate(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return "Long analysis about the patch proposal without valid JSON."
            return """
{
  "summary": "Prepared a bounded patch proposal.",
  "deliverable": "Converted the candidate patch into structured fields.",
  "verification": "Main agent should review the proposal before applying it.",
  "implementation_plan": ["Update router metadata", "Adjust executor prompt"],
  "proposed_patch": [
    {"path": "app/routers/models.py", "change_type": "update", "summary": "Refine dispatch payload"},
    {"path": "app/services/handoff_packet_executor.py", "change_type": "update", "summary": "Strengthen structured output handling"}
  ]
}
"""

    gateway = _FakeGateway()
    result = await execute_handoff_packet(
        {
            "task_description": "Prepare a bounded patch proposal",
            "task_type": "code_generation",
            "execution_mode": "economy",
            "recommended_executor": "cheap_subagent",
            "recommended_model": "glm-4.7",
            "definition_of_done": "Produce a patch proposal",
            "expected_output_shape": "Short proposed patch summary",
            "write_scope": ["app/routers/models.py"],
        },
        gateway,
    )

    assert len(gateway.calls) == 2
    assert "Convert the following worker output into one JSON object only." in gateway.calls[1]["prompt"]
    assert result["structured"] is True
    assert result["summary"] == "Prepared a bounded patch proposal."
    assert len(result["proposed_patch"]) == 2


@pytest.mark.asyncio
async def test_execute_handoff_packet_falls_back_to_secondary_cloud_model():
    class _FakeGateway:
        local_model = "qwen3:1.7b"

        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def generate(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs.get("model_override") == "gemini-3.1-flash":
                raise RuntimeError("404 Not Found")
            return """
{
  "summary": "Prepared a bounded patch proposal.",
  "deliverable": "Used fallback cloud model.",
  "verification": "Main agent should review the fallback-produced plan.",
  "implementation_plan": ["Update router metadata"],
  "proposed_patch": [
    {"path": "app/routers/models.py", "change_type": "update", "summary": "Refine dispatch payload"}
  ]
}
"""

    gateway = _FakeGateway()
    result = await execute_handoff_packet(
        {
            "task_description": "Prepare a bounded patch proposal",
            "task_type": "code_generation",
            "execution_mode": "economy",
            "recommended_executor": "cheap_subagent",
            "recommended_model": "gemini-3.1-flash",
            "routing_basis": {"cloud_fallbacks": [{"model_id": "glm-4.7"}]},
            "definition_of_done": "Produce a patch proposal",
            "expected_output_shape": "Short proposed patch summary",
            "write_scope": ["app/routers/models.py"],
        },
        gateway,
    )

    assert [call["model_override"] for call in gateway.calls[:2]] == ["gemini-3.1-flash", "glm-4.7"]
    assert result["structured"] is True
    assert result["model_used"] == "glm-4.7"


@pytest.mark.asyncio
async def test_execute_handoff_packet_uses_gateway_candidates_after_routing_candidates_fail():
    class _FakeGateway:
        local_model = "qwen3:1.7b"

        def __init__(self) -> None:
            self.calls: list[dict] = []

        def _candidate_models(self, mode, *, task_type=None):
            return ["gemini-3.1-flash", "deepseek-chat"]

        async def generate(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs.get("model_override") == "gemini-3.1-flash":
                raise RuntimeError("404 Not Found")
            return """
{
  "summary": "Prepared a bounded patch proposal.",
  "deliverable": "Used DeepSeek after Gemini was unavailable.",
  "verification": "Main agent should review the fallback-produced plan.",
  "implementation_plan": ["Update router metadata"],
  "proposed_patch": [
    {"path": "app/routers/models.py", "change_type": "update", "summary": "Refine dispatch payload"}
  ]
}
"""

    gateway = _FakeGateway()
    result = await execute_handoff_packet(
        {
            "task_description": "Prepare a bounded patch proposal",
            "task_type": "code_generation",
            "execution_mode": "economy",
            "recommended_executor": "cheap_subagent",
            "recommended_model": "gemini-3.1-flash",
            "definition_of_done": "Produce a patch proposal",
            "expected_output_shape": "Short proposed patch summary",
            "write_scope": ["app/routers/models.py"],
        },
        gateway,
    )

    assert [call["model_override"] for call in gateway.calls[:2]] == ["gemini-3.1-flash", "deepseek-chat"]
    assert result["structured"] is True
    assert result["model_used"] == "deepseek-chat"


@pytest.mark.asyncio
async def test_execute_handoff_packet_uses_local_fallback_after_cloud_candidates_fail():
    class _FakeGateway:
        local_model = "qwen3:1.7b"

        def __init__(self) -> None:
            self.calls: list[dict] = []

        def _candidate_models(self, mode, *, task_type=None):
            return ["gemini-3.1-flash"]

        async def generate(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs.get("allow_local_fallback"):
                return """
{
  "summary": "Prepared a local fallback patch proposal.",
  "deliverable": "Used local Ollama after cloud candidates were unavailable.",
  "verification": "Main agent should review the fallback-produced plan.",
  "implementation_plan": ["Update router metadata"],
  "proposed_patch": [
    {"path": "app/routers/models.py", "change_type": "update", "summary": "Refine dispatch payload"}
  ]
}
"""
            raise RuntimeError("404 Not Found")

    gateway = _FakeGateway()
    result = await execute_handoff_packet(
        {
            "task_description": "Prepare a bounded patch proposal",
            "task_type": "code_generation",
            "execution_mode": "economy",
            "recommended_executor": "cheap_subagent",
            "recommended_model": "gemini-3.1-flash",
            "definition_of_done": "Produce a patch proposal",
            "expected_output_shape": "Short proposed patch summary",
            "write_scope": ["app/routers/models.py"],
        },
        gateway,
    )

    assert gateway.calls[-1]["allow_local_fallback"] is True
    assert gateway.calls[-1]["prefer_local"] is True
    assert result["structured"] is True
    assert result["model_used"] == "qwen3:1.7b"
