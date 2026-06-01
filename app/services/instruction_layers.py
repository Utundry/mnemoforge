"""
Instruction Layers Service for MCP Layered Instructions System (L0-L4)

This service provides:
- L0: Core policy (immutable, always present)
- L1: Task summary (dynamic)
- L2: Category-specific guidelines (auto-loaded)
- L3: Detailed reference (on-demand)
- L4: Advanced/experimental (opt-in)
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

# Project root for resolving layer file paths
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_LAYERS_DIR = _PROJECT_ROOT / "docs" / "instructions" / "layers"


# L2 Category definitions with domain patterns for auto-detection
_L2_CATEGORIES: dict[str, dict[str, Any]] = {
    "memory_operations": {
        "name": "Memory Operations",
        "description": "Guidelines for memory search, storage, and retrieval",
        "domain_patterns": ["memory", "search", "store", "retrieve", "qdrant"],
        "tool_patterns": ["memory_", "ingest", "tree"],
    },
    "skills": {
        "name": "Skills",
        "description": "Guidelines for skill marketplace operations",
        "domain_patterns": ["skill", "marketplace", "reusable"],
        "tool_patterns": ["skill_", "crystallize"],
    },
    "coordination": {
        "name": "Coordination",
        "description": "Guidelines for agent-to-agent communication",
        "domain_patterns": ["coordination", "message", "communication"],
        "tool_patterns": ["coordination", "send_message", "pickup_message"],
    },
    "governance": {
        "name": "Governance",
        "description": "Guidelines for project laws, improvements, and canonicals",
        "domain_patterns": ["law", "governance", "rule", "improvement", "canonical"],
        "tool_patterns": ["law", "improvement", "canonical", "resolve"],
    },
    "project_bootstrap": {
        "name": "Project Bootstrap",
        "description": "Guidelines for external project setup",
        "domain_patterns": ["bootstrap", "setup", "external", "pilot"],
        "tool_patterns": ["bootstrap", "readiness", "checklist"],
    },
    "handoff": {
        "name": "Handoff",
        "description": "Guidelines for task handoff between agents",
        "domain_patterns": ["handoff", "task", "packet"],
        "tool_patterns": ["handoff", "pickup", "dispatch"],
    },
}


def build_l0_policy() -> str:
    """
    Generate L0 Core Policy Layer.

    This layer is immutable and always present. It contains
    critical safety constraints and fundamental agent behavior.
    """
    return """## L0: Core Policy

You are an AI agent working through the SloplessCode MCP server. You MUST:

1. **Safety First**: Never execute harmful, illegal, or malicious code
2. **Respect Privacy**: Do not expose sensitive user data without explicit permission
3. **Verify Actions**: Confirm destructive operations before executing
4. **Report Errors**: Always surface errors clearly to the user
5. **Use Tools Appropriately**: Only use available MCP tools for their intended purpose
6. **Maintain Context**: Keep project_id consistent across related operations
7. **Prefer Semantic Routes**: Use /api/v1/coordination/... over internal module topology"""


def build_l1_summary(
    task_description: str = "",
    priority: str = "normal",
    phase: str = "",
    next_steps: list[str] | None = None,
) -> str:
    """
    Generate L1 Task Summary Layer.

    This layer provides concise task context and immediate guidance.

    Args:
        task_description: Current task description
        priority: Task priority (high, normal, low)
        phase: Current phase (e.g., task_framing, implementation)
        next_steps: List of immediate next steps
    """
    lines = ["## L1: Task Context"]

    if task_description:
        lines.append(f"\nCurrent Task: {task_description}")

    if priority:
        lines.append(f"Priority: {priority}")

    if phase:
        lines.append(f"Phase: {phase}")

    if next_steps:
        lines.append("\nNext Steps:")
        for step in next_steps[:3]:  # Max 3 steps
            lines.append(f"- {step}")

    return "\n".join(lines)


def get_l2_layer(category: str) -> str:
    """
    Load L2 Category-Specific Layer.

    Args:
        category: L2 category (e.g., 'memory_operations', 'skills')

    Returns:
        Layer content as markdown string

    Raises:
        FileNotFoundError: If layer file doesn't exist
    """
    if category not in _L2_CATEGORIES:
        return f"## L2: {category}\n\nNo guidelines available for this category."

    layer_file = _LAYERS_DIR / "L2" / f"{category}.md"

    if not layer_file.exists():
        # Return placeholder if file doesn't exist yet
        cat_info = _L2_CATEGORIES[category]
        return f"""## L2: {cat_info['name']}

{cat_info['description']}

*Guidelines for this category are being developed. In the meantime, refer to the tool descriptions and call load_instruction_layer(layer="L3", category="{category}") for detailed reference.*"""

    return layer_file.read_text(encoding="utf-8")


def infer_instruction_category(
    task_description: str = "",
    tools_called: list[str] | None = None,
    domains: list[str] | None = None,
) -> str:
    """
    Infer the appropriate L2 category from context.

    Priority order:
    1. Explicit category in task description
    2. Domain patterns from task description
    3. Tool patterns from tools called
    4. Domain patterns from inferred domains

    Args:
        task_description: Current task description
        tools_called: List of tool names called in session
        domains: List of inferred domains

    Returns:
        Category name (e.g., 'memory_operations')
    """
    if not task_description and not tools_called and not domains:
        return "memory_operations"  # Default fallback

    task_lower = task_description.lower() if task_description else ""
    tools_lower = [t.lower() for t in (tools_called or [])]
    domains_lower = [d.lower() for d in (domains or [])]

    # Score each category based on matches
    scores: dict[str, int] = {}

    for category, info in _L2_CATEGORIES.items():
        score = 0

        # Check domain patterns in task description
        for pattern in info["domain_patterns"]:
            if pattern.lower() in task_lower:
                score += 3

        # Check domain patterns in inferred domains
        for pattern in info["domain_patterns"]:
            if any(pattern.lower() in d for d in domains_lower):
                score += 2

        # Check tool patterns
        for pattern in info["tool_patterns"]:
            if any(pattern.lower() in t for t in tools_lower):
                score += 2

        if score > 0:
            scores[category] = score

    # Return highest-scoring category, or default
    if scores:
        return max(scores.items(), key=lambda x: x[1])[0]

    return "memory_operations"  # Default fallback


def get_l3_layer(category: str, section: str = "api_reference") -> str:
    """
    Load L3 Detailed Reference Layer.

    Args:
        category: L2 category (e.g., 'memory_operations')
        section: Section to load (api_reference, examples, troubleshooting)

    Returns:
        Layer content as markdown string
    """
    layer_file = _LAYERS_DIR / "L3" / category / f"{section}.md"

    if not layer_file.exists():
        return f"""## L3: {category} - {section}

*Detailed reference for this section is being developed.*

In the meantime, refer to the tool descriptions and use the MCP tools' built-in help."""

    return layer_file.read_text(encoding="utf-8")


def get_l4_layer(section: str = "advanced_patterns") -> str:
    """
    Load L4 Advanced/Experimental Layer.

    Args:
        section: Section to load (advanced_patterns, experimental_features)

    Returns:
        Layer content as markdown string
    """
    layer_file = _LAYERS_DIR / "L4" / f"{section}.md"

    if not layer_file.exists():
        return """## L4: Advanced Patterns (Experimental)

⚠️ **WARNING**: Advanced patterns are experimental and may change.

*Advanced patterns are being developed. Check back later for cutting-edge features and optimization techniques.*"""

    return layer_file.read_text(encoding="utf-8")


def list_available_layers(layer: str | None = None) -> dict[str, Any]:
    """
    List available instruction layers.

    Args:
        layer: Filter by layer (L2, L3, L4). If None, returns all.

    Returns:
        Dictionary with layer information
    """
    result: dict[str, Any] = {}

    if layer is None or layer == "L2":
        result["L2"] = [
            {
                "category": cat,
                "name": info["name"],
                "description": info["description"],
            }
            for cat, info in _L2_CATEGORIES.items()
        ]

    if layer is None or layer == "L3":
        l3_dir = _LAYERS_DIR / "L3"
        if l3_dir.exists():
            result["L3"] = []
            for category_dir in l3_dir.iterdir():
                if category_dir.is_dir():
                    sections = [f.stem for f in category_dir.glob("*.md")]
                    result["L3"].append({
                        "category": category_dir.name,
                        "sections": sections,
                    })

    if layer is None or layer == "L4":
        l4_dir = _LAYERS_DIR / "L4"
        if l4_dir.exists():
            result["L4"] = [f.stem for f in l4_dir.glob("*.md")]

    return result


def build_layered_onboarding(
    task_description: str = "",
    priority: str = "normal",
    phase: str = "",
    next_steps: list[str] | None = None,
    tools_called: list[str] | None = None,
    domains: list[str] | None = None,
    include_l2: bool = True,
) -> str:
    """
    Build complete onboarding response with layered instructions.

    Args:
        task_description: Current task description
        priority: Task priority
        phase: Current phase
        next_steps: List of immediate next steps
        tools_called: List of tools called (for category inference)
        domains: List of inferred domains
        include_l2: Whether to include L2 layer

    Returns:
        Complete onboarding text with L0, L1, and optionally L2
    """
    sections: list[str] = []

    # L0: Always present
    sections.append(build_l0_policy())

    # L1: Task summary
    l1 = build_l1_summary(task_description, priority, phase, next_steps)
    if l1 != "## L1: Task Context":  # Only add if there's content
        sections.append(l1)

    # L2: Auto-loaded category
    if include_l2:
        category = infer_instruction_category(task_description, tools_called, domains)
        l2 = get_l2_layer(category)
        sections.append(l2)

        # Add tip for L3
        sections.append(
            "\n---\n\n"
            f"TIP: Call `load_instruction_layer(layer=\"L3\", category=\"{category}\")` "
            "for detailed API reference."
        )

    return "\n\n".join(sections)
