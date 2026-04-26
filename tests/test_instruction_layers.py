"""
Tests for instruction layers system:
  1. L0 policy generation
  2. L1 summary generation
  3. L2 layer loading
  4. Category inference
  5. Layered onboarding
  6. L3/L4 placeholder layers
  7. List available layers
  8. MCP round-trip tests
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from app.services.instruction_layers import (
    build_l0_policy,
    build_l1_summary,
    get_l2_layer,
    infer_instruction_category,
    get_l3_layer,
    get_l4_layer,
    list_available_layers,
    build_layered_onboarding,
    _L2_CATEGORIES,
)


# ── L0 Policy Tests ────────────────────────────────────────────────────────────


class TestL0Policy:
    def test_build_l0_policy_returns_string(self):
        """build_l0_policy returns a non-empty string."""
        policy = build_l0_policy()
        assert isinstance(policy, str)
        assert len(policy) > 0

    def test_build_l0_policy_contains_core_elements(self):
        """L0 policy contains required safety and behavioral elements."""
        policy = build_l0_policy()
        assert "L0: Core Policy" in policy
        assert "Safety First" in policy
        assert "Respect Privacy" in policy
        assert "Verify Actions" in policy
        assert "Report Errors" in policy
        assert "Use Tools Appropriately" in policy

    def test_build_l0_policy_is_deterministic(self):
        """L0 policy is deterministic (same content on multiple calls)."""
        policy1 = build_l0_policy()
        policy2 = build_l0_policy()
        assert policy1 == policy2


# ── L1 Summary Tests ───────────────────────────────────────────────────────────


class TestL1Summary:
    def test_build_l1_summary_basic(self):
        """build_l1_summary creates a summary from basic parameters."""
        summary = build_l1_summary(
            task_description="Test task",
            priority="high",
            phase="implementation",
        )
        assert "L1: Task Context" in summary
        assert "Test task" in summary
        assert "high" in summary
        assert "implementation" in summary

    def test_build_l1_summary_with_next_steps(self):
        """build_l1_summary includes next steps when provided."""
        summary = build_l1_summary(
            task_description="Test task",
            priority="normal",
            phase="planning",
            next_steps=["Step 1", "Step 2"],
        )
        assert "Step 1" in summary
        assert "Step 2" in summary

    def test_build_l1_summary_empty_params(self):
        """build_l1_summary handles empty/None parameters gracefully."""
        summary = build_l1_summary(
            task_description="",
            priority="",
            phase="",
        )
        assert "L1: Task Context" in summary
        # Should not crash with empty values

    def test_build_l1_summary_none_params(self):
        """build_l1_summary handles None parameters gracefully."""
        summary = build_l1_summary(
            task_description=None,
            priority=None,
            phase=None,
        )
        assert "L1: Task Context" in summary
        # Should not crash with None values


# ── L2 Layer Tests ────────────────────────────────────────────────────────────


class TestL2Layer:
    def test_get_l2_layer_memory_operations(self):
        """get_l2_layer returns content for memory_operations category."""
        layer = get_l2_layer("memory_operations")
        assert isinstance(layer, str)
        assert len(layer) > 0
        assert "memory" in layer.lower()

    def test_get_l2_layer_skills(self):
        """get_l2_layer returns content for skills category."""
        layer = get_l2_layer("skills")
        assert isinstance(layer, str)
        assert len(layer) > 0
        assert "skill" in layer.lower()

    def test_get_l2_layer_coordination(self):
        """get_l2_layer returns content for coordination category."""
        layer = get_l2_layer("coordination")
        assert isinstance(layer, str)
        assert len(layer) > 0
        assert "coordination" in layer.lower()

    def test_get_l2_layer_governance(self):
        """get_l2_layer returns content for governance category."""
        layer = get_l2_layer("governance")
        assert isinstance(layer, str)
        assert len(layer) > 0
        assert "governance" in layer.lower()

    def test_get_l2_layer_project_bootstrap(self):
        """get_l2_layer returns content for project_bootstrap category."""
        layer = get_l2_layer("project_bootstrap")
        assert isinstance(layer, str)
        assert len(layer) > 0
        assert "bootstrap" in layer.lower()

    def test_get_l2_layer_handoff(self):
        """get_l2_layer returns content for handoff category."""
        layer = get_l2_layer("handoff")
        assert isinstance(layer, str)
        assert len(layer) > 0
        assert "handoff" in layer.lower()

    def test_get_l2_layer_invalid_category(self):
        """get_l2_layer returns placeholder for invalid category."""
        layer = get_l2_layer("invalid_category")
        assert isinstance(layer, str)
        assert len(layer) > 0
        # Should return a generic or error message

    def test_l2_categories_defined(self):
        """All L2 categories are defined in _L2_CATEGORIES."""
        expected_categories = [
            "memory_operations",
            "skills",
            "coordination",
            "governance",
            "project_bootstrap",
            "handoff",
        ]
        for cat in expected_categories:
            assert cat in _L2_CATEGORIES
            assert "domain_patterns" in _L2_CATEGORIES[cat]
            assert "tool_patterns" in _L2_CATEGORIES[cat]


# ── Category Inference Tests ──────────────────────────────────────────────────


class TestCategoryInference:
    def test_infer_memory_category_from_description(self):
        """infer_instruction_category detects memory operations from task description."""
        category = infer_instruction_category(
            task_description="Search for memories about encoding issues",
            tools_called=["memory_search"],
        )
        assert category == "memory_operations"

    def test_infer_skills_category_from_description(self):
        """infer_instruction_category detects skills from task description."""
        category = infer_instruction_category(
            task_description="Install a new skill from the marketplace",
            tools_called=["skill_install"],
        )
        assert category == "skills"

    def test_infer_governance_category_from_description(self):
        """infer_instruction_category detects governance from task description."""
        category = infer_instruction_category(
            task_description="Review and approve pending project laws",
            tools_called=["list_project_laws"],
        )
        assert category == "governance"

    def test_infer_coordination_category_from_description(self):
        """infer_instruction_category detects coordination from task description."""
        category = infer_instruction_category(
            task_description="Send a coordination message to another agent",
            tools_called=["send_coordination_message"],
        )
        assert category == "coordination"

    def test_infer_handoff_category_from_description(self):
        """infer_instruction_category detects handoff from task description."""
        category = infer_instruction_category(
            task_description="Hand off this task to another agent",
            tools_called=["handoff_task"],
        )
        assert category == "handoff"

    def test_infer_project_bootstrap_category_from_description(self):
        """infer_instruction_category detects project bootstrap from task description."""
        category = infer_instruction_category(
            task_description="Bootstrap a new external project",
            tools_called=["get_project_bootstrap_checklist"],
        )
        assert category == "project_bootstrap"

    def test_infer_from_tools_only(self):
        """infer_instruction_category can infer from tools_called only."""
        category = infer_instruction_category(
            task_description="",
            tools_called=["memory_search", "memory_store"],
        )
        assert category == "memory_operations"

    def test_infer_returns_default_when_no_match(self):
        """infer_instruction_category returns default category when no match."""
        category = infer_instruction_category(
            task_description="Do something unrelated",
            tools_called=[],
            domains=[],
        )
        # Should return memory_operations as default
        assert category == "memory_operations"

    def test_infer_explicit_category_overrides_inference(self):
        """Domain patterns in task description override tool patterns."""
        # If task description strongly indicates one category, it should win
        category = infer_instruction_category(
            task_description="Install a skill from the marketplace",
            tools_called=["memory_search"],  # tools suggest memory, but task says skills
            domains=[],
        )
        assert category == "skills"


# ── L3/L4 Layer Tests ─────────────────────────────────────────────────────────


class TestL3L4Layers:
    def test_get_l3_layer_returns_placeholder(self):
        """get_l3_layer returns a placeholder message (not yet implemented)."""
        layer = get_l3_layer("memory_operations", "api_reference")
        assert isinstance(layer, str)
        assert len(layer) > 0
        # Should indicate it's not implemented yet
        assert "being developed" in layer.lower()

    def test_get_l4_layer_returns_placeholder(self):
        """get_l4_layer returns a placeholder message (not yet implemented)."""
        layer = get_l4_layer("advanced_patterns")
        assert isinstance(layer, str)
        assert len(layer) > 0
        # Should indicate it's not implemented yet
        assert "being developed" in layer.lower()


# ── List Available Layers Tests ───────────────────────────────────────────────


class TestListAvailableLayers:
    def test_list_all_layers(self):
        """list_available_layers returns all layers when no filter."""
        result = list_available_layers()
        assert isinstance(result, dict)
        assert "L2" in result
        # L3 and L4 may not be in result if not implemented
        # Just check that result is a dict with L2

    def test_list_l2_layers_only(self):
        """list_available_layers returns only L2 layers when filtered."""
        result = list_available_layers(layer="L2")
        assert isinstance(result, dict)
        assert "L2" in result
        # Should have at least some L2 categories
        assert len(result["L2"]) > 0

    def test_list_l3_layers_only(self):
        """list_available_layers returns only L3 layers when filtered."""
        result = list_available_layers(layer="L3")
        assert isinstance(result, dict)
        # L3 may return empty or indicate not implemented

    def test_list_l4_layers_only(self):
        """list_available_layers returns only L4 layers when filtered."""
        result = list_available_layers(layer="L4")
        assert isinstance(result, dict)
        # L4 may return empty or indicate not implemented


# ── Layered Onboarding Tests ──────────────────────────────────────────────────


class TestLayeredOnboarding:
    def test_build_layered_onboarding_basic(self):
        """build_layered_onboarding creates a complete onboarding package."""
        onboarding = build_layered_onboarding(
            task_description="Test task",
            priority="high",
            phase="implementation",
            domains=["memory", "search"],
        )
        assert isinstance(onboarding, str)
        assert len(onboarding) > 0
        assert "L0: Core Policy" in onboarding
        assert "L1: Task Context" in onboarding
        # L2 layer should be included
        assert "L2:" in onboarding

    def test_build_layered_onboarding_with_domains(self):
        """build_layered_onboarding uses domains for category inference."""
        onboarding = build_layered_onboarding(
            task_description="Test task",
            priority="normal",
            phase="planning",
            domains=["skills", "marketplace"],
        )
        # L2 layer should be included
        assert "L2:" in onboarding

    def test_build_layered_onboarding_infers_category(self):
        """build_layered_onboarding infers category from domains when not provided."""
        onboarding = build_layered_onboarding(
            task_description="Review project laws and improvements",
            priority="normal",
            phase="planning",
            domains=["governance", "laws"],
        )
        # L2 layer should be included
        assert "L2:" in onboarding

    def test_build_layered_onboarding_empty_params(self):
        """build_layered_onboarding handles empty parameters gracefully."""
        onboarding = build_layered_onboarding(
            task_description="",
            priority="",
            phase="",
            domains=[],
        )
        assert isinstance(onboarding, str)
        assert len(onboarding) > 0
        # Should still include L0 at minimum
        assert "L0: Core Policy" in onboarding

    def test_build_layered_onboarding_without_l2(self):
        """build_layered_onboarding can exclude L2 layer."""
        onboarding = build_layered_onboarding(
            task_description="Test task",
            priority="normal",
            phase="planning",
            domains=["memory"],
            include_l2=False,
        )
        assert isinstance(onboarding, str)
        assert "L0: Core Policy" in onboarding
        # L2 should not be included
        assert "L2:" not in onboarding


# ── MCP Round-Trip Tests ──────────────────────────────────────────────────────


class TestMCPInstructionLayerRoundTrip:
    """End-to-end tests for MCP instruction layer tools through SSE transport."""

    @pytest.mark.asyncio
    async def test_load_instruction_layer_l3_round_trip(self, monkeypatch):
        """load_instruction_layer tool returns L3 layer content via MCP round-trip."""
        from app.routers import mcp_sse

        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "load_instruction_layer",
            {"layer": "L3", "category": "memory_operations"},
            "http://test",
        )

        assert isinstance(result, str)
        assert "L3:" in result
        assert "memory_operations" in result.lower()
        # Should indicate it's being developed
        assert "being developed" in result.lower()

    @pytest.mark.asyncio
    async def test_load_instruction_layer_l4_round_trip(self, monkeypatch):
        """load_instruction_layer tool returns L4 layer content via MCP round-trip."""
        from app.routers import mcp_sse

        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "load_instruction_layer",
            {"layer": "L4"},
            "http://test",
        )

        assert isinstance(result, str)
        assert "L4:" in result
        # Should indicate it's experimental
        assert "experimental" in result.lower()

    @pytest.mark.asyncio
    async def test_load_instruction_layer_invalid_layer_round_trip(self, monkeypatch):
        """load_instruction_layer returns error for invalid layer via MCP round-trip."""
        from app.routers import mcp_sse

        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "load_instruction_layer",
            {"layer": "L5", "category": "memory_operations"},
            "http://test",
        )

        # Should return an error message
        assert isinstance(result, str)
        assert "error" in result.lower() or "invalid" in result.lower()

    @pytest.mark.asyncio
    async def test_list_instruction_layers_all_round_trip(self, monkeypatch):
        """list_instruction_layers tool returns all available layers via MCP round-trip."""
        from app.routers import mcp_sse

        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "list_instruction_layers",
            {},
            "http://test",
        )

        assert isinstance(result, str)
        # Should include L2 layers
        assert "## L2" in result
        # Should list at least some categories
        assert "memory_operations" in result.lower() or "skills" in result.lower()

    @pytest.mark.asyncio
    async def test_list_instruction_layers_filtered_round_trip(self, monkeypatch):
        """list_instruction_layers tool returns filtered layers via MCP round-trip."""
        from app.routers import mcp_sse

        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "list_instruction_layers",
            {"layer": "L2"},
            "http://test",
        )

        assert isinstance(result, str)
        assert "## L2" in result
        # Should list L2 categories
        assert "memory_operations" in result.lower() or "skills" in result.lower()

    @pytest.mark.asyncio
    async def test_get_onboarding_with_instruction_layers_round_trip(self, monkeypatch):
        """get_onboarding includes L0 and L2 layers via MCP round-trip."""
        from app.routers import mcp_sse

        async def fake_post(api_base: str, path: str, payload: dict):
            if path == "/skills/profile":
                return {"domains": ["memory", "search"]}
            if path == "/skills/pack/create":
                return {
                    "pack_id": "pack-1",
                    "degraded": False,
                    "skills": [],
                }
            if path == "/admin/storage-trust":
                return {"status": "ok", "summary": "ok"}
            if path == "/skills/pinned":
                return []
            if path.startswith("/skills/gaps"):
                return {"gaps": []}
            if path.startswith("/skills/analytics"):
                return {"total_outcomes": 0}
            if path.startswith("/memories/recent"):
                return []
            raise AssertionError(f"unexpected POST path: {path}")

        async def fake_get(api_base: str, path: str):
            if path == "/admin/storage-trust":
                return {"status": "ok", "summary": "ok"}
            if path == "/skills/pinned":
                return []
            if path.startswith("/skills/gaps"):
                return {"gaps": []}
            if path.startswith("/skills/analytics"):
                return {"total_outcomes": 0}
            if path.startswith("/memories/recent"):
                return []
            raise AssertionError(f"unexpected GET path: {path}")

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "get_onboarding",
            {"agent_id": "test-agent", "task_description": "Search for memories"},
            "http://test",
        )

        assert isinstance(result, str)
        # Should include L0
        assert "L0: Core Policy" in result
        # Should include L2 based on inferred category (memory_operations)
        assert "L2:" in result
        # Should have exactly one L2 layer (no double inference)
        l2_count = result.count("L2:")
        assert l2_count == 1, f"Expected 1 L2 layer, found {l2_count}"
