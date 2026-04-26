# Layered Instructions System - Quick Start

## What is this?

The Layered Instructions System provides structured, context-aware instructions to AI agents through the MCP protocol. Instructions are divided into layers that load dynamically based on task context.

## Current Status

**Implemented:**
- **L0**: Core safety policy (immutable, always present)
- **L1**: Task summary (dynamic)
- **L2**: Category-specific guidelines (auto-loaded)

**Not Yet Implemented:**
- **L3**: Detailed API reference (planned)
- **L4**: Advanced/experimental features (planned)

## Quick Reference

| Layer | When Loaded | Purpose | Status |
|-------|-------------|---------|--------|
| **L0** | Always | Core safety policy (immutable) | ✅ Implemented |
| **L1** | Always | Task summary and next steps | ✅ Implemented |
| **L2** | Auto | Category-specific guidelines | ✅ Implemented |
| **L3** | On-demand | Detailed API reference | ⏳ Planned |
| **L4** | Opt-in | Advanced/experimental features | ⏳ Planned |

## For Agents

### Getting Started

1. **Call `get_onboarding` at session start**:
   ```python
   get_onboarding(agent_id="your-agent-id", task_description="your task")
   ```

2. **You'll receive L0, L1, and L2 automatically** - no manual setup needed!

3. **Load L3 for details when needed**:
   ```python
   load_instruction_layer(layer="L3", category="memory_operations")
   ```

4. **Load L4 for advanced patterns** (optional):
   ```python
   load_instruction_layer(layer="L4")
   ```

### Example Session

```python
# 1. Start session
onboarding = get_onboarding(
    agent_id="claude-code",
    task_description="Search for memories about mojibake encoding"
)

# Response includes:
# - L0: Core Policy
# - L1: Task Summary
# - L2: Memory Operations Guidelines (auto-detected)

# 2. Do work...
results = memory_search(query="mojibake encoding fix")

# 3. Need detailed API reference?
reference = load_instruction_layer(layer="L3", category="memory_operations")

# 4. Done? Record outcome
record_outcome(
    agent_id="claude-code",
    pack_id=onboarding.get("pack_id"),
    success=True
)
```

## For Developers

### Adding a New L2 Category

1. Create file: `docs/instructions/layers/L2/your_category.md`
2. Follow the template:
   ```markdown
   ## L2: Your Category Guidelines

   When working with [category] tools:

   1. **Guideline 1**: Description
   2. **Guideline 2**: Description

   Common Patterns:
   - Pattern 1
   - Pattern 2
   ```
3. Add category mapping in `app/services/instruction_layers.py`

### Adding L3 Reference Docs

1. Create directory: `docs/instructions/layers/L3/your_category/`
2. Add files:
   - `api_reference.md` - Detailed API docs
   - `examples.md` - Code examples
   - `troubleshooting.md` - Common issues

### File Structure

```
docs/instructions/
├── README.md                          # This file
├── LAYERED_INSTRUCTIONS_DESIGN.md    # Full design spec
└── layers/
    ├── L0/
    │   └── core_policy.md             # Auto-generated
    ├── L2/
    │   ├── memory_operations.md
    │   ├── skills.md
    │   ├── coordination.md
    │   ├── governance.md
    │   ├── project_bootstrap.md
    │   └── handoff.md
    ├── L3/
    │   └── [category]/
    │       ├── api_reference.md
    │       ├── examples.md
    │       └── troubleshooting.md
    └── L4/
        ├── advanced_patterns.md
        └── experimental_features.md
```

## Available L2 Categories

| Category | When Auto-Loaded |
|----------|-----------------|
| `memory_operations` | Task involves memory search/storage |
| `skills` | Task involves skill marketplace |
| `coordination` | Task involves agent communication |
| `governance` | Task involves laws/improvements |
| `project_bootstrap` | Task involves project setup |
| `handoff` | Task involves task handoff |

## MCP Tools

### `load_instruction_layer`

Load a specific instruction layer on demand.

```python
load_instruction_layer(
    layer="L3",              # "L3" or "L4"
    category="memory_operations"  # Required for L3
)
```

### `list_instruction_layers`

List available instruction layers.

```python
list_instruction_layers(layer="L2")  # Optional: filter by layer
```

## Design Principles

1. **Strict Instruction Following**: L0 policy cannot be overridden
2. **Automatic Context Loading**: L2 loads automatically based on task
3. **On-Demand Detail**: L3/L4 only when explicitly requested
4. **Tool-Instruction Separation**: MCP tools are separate from instructions
5. **No Manual Profile Switching**: System auto-selects appropriate layers

## Full Documentation

See [LAYERED_INSTRUCTIONS_DESIGN.md](./LAYERED_INSTRUCTIONS_DESIGN.md) for complete design specification, implementation details, and examples.

## Support

- Issues: Report via MCP `report_issue` tool
- Questions: Use MCP coordination messages
- Documentation: See full design spec above
