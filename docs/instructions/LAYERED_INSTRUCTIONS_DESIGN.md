# MCP Layered Instructions System (L0-L2)

## Overview

The MCP Layered Instructions System provides a structured approach to delivering context-aware instructions to AI agents through the MCP protocol. The system divides instructions into three layers (L0-L2) that are loaded dynamically based on task context, agent state, and user requirements.

**Current Implementation Status:**
- ✅ L0: Core Policy (immutable, always present)
- ✅ L1: Task Summary (dynamic)
- ✅ L2: Category-Specific Guidelines (auto-loaded)
- ⏳ L3: Detailed Reference (planned, not yet implemented)
- ⏳ L4: Advanced/Experimental (planned, not yet implemented)

## Design Principles

1. **Strict Instruction Following**: L0 policy block is immutable and always present
2. **Automatic Context Loading**: L2 blocks are loaded automatically based on task category
3. **Tool-Instruction Separation**: MCP tool exposure is independent from instruction packs
4. **No Manual Profile Switching**: The system automatically selects appropriate layers

**Note:** L3/L4 layers (on-demand loading) are planned but not yet implemented. Currently, only L0-L2 are available.

## Layer Structure

### L0: Core Policy Layer (Always Present)

**Purpose**: Immutable safety and behavioral guardrails

**Characteristics**:
- Always included in every response
- Cannot be overridden by other layers
- Contains critical safety constraints
- Defines fundamental agent behavior

**Content**:
```markdown
## L0: Core Policy

You are an AI agent working through the SuperMemory MCP server. You MUST:

1. **Safety First**: Never execute harmful, illegal, or malicious code
2. **Respect Privacy**: Do not expose sensitive user data without explicit permission
3. **Verify Actions**: Confirm destructive operations before executing
4. **Report Errors**: Always surface errors clearly to the user
5. **Use Tools Appropriately**: Only use available MCP tools for their intended purpose
```

**Implementation**: Injected into `get_onboarding` response and `build_supermemory_initialize_hint`

---

### L1: Task Summary Layer

**Purpose**: Concise task context and immediate guidance

**Characteristics**:
- Generated dynamically from task description
- Brief (max 200-300 characters)
- Focuses on immediate next steps
- Updated per task

**Content**:
```markdown
## L1: Task Context

Current Task: {task_description}

Priority: {priority}
Phase: {phase}

Next Steps:
- {next_step_1}
- {next_step_2}
```

**Implementation**: Extracted from handoff packets or task enrichment context

---

### L2: Category-Specific Layer (Auto-Loaded)

**Purpose**: Domain-specific knowledge and patterns

**Characteristics**:
- Automatically selected based on task category/domain
- Loaded without explicit request
- Contains reusable patterns for the domain
- Cached for performance

**Categories**:
- `memory_operations`: Memory search, storage, retrieval
- `skills`: Skill marketplace, installation, publishing
- `coordination`: Agent-to-agent communication
- `governance`: Project laws, improvements, canonicals
- `project_bootstrap`: External project setup
- `handoff`: Task handoff between agents

**Example (memory_operations)**:
```markdown
## L2: Memory Operations Guidelines

When working with memory tools:

1. **Prefer Semantic Search**: Use `memory_search` with natural language queries
2. **Batch When Possible**: Use `memory_batch_store` for multiple memories
3. **Context Matters**: Use `memory_context` for task-aware retrieval
4. **Track Sessions**: Provide `session_id` for related operations

Common Patterns:
- Start with `enrich_task_with_context` for project tasks
- Use `memory_tree_slice` for hierarchical knowledge
- Call `memory_cleanup` periodically for maintenance
```

**Implementation**: Stored in `app/services/instruction_layers.py`, loaded based on `task_category` or inferred domain

---

### L3: Detailed Reference Layer (On-Demand)

**Purpose**: Comprehensive documentation and examples

**Characteristics**:
- Only loaded when explicitly requested
- Contains detailed API documentation
- Includes code examples
- Covers edge cases and troubleshooting

**Content**:
```markdown
## L3: Detailed Reference

### Memory Search API

**Tool**: `memory_search`

**Parameters**:
- `query` (required): Natural language search query
- `limit` (optional): Max results (default: 5)
- `memory_type` (optional): Filter by type
- `category` (optional): Filter by category
- `min_score` (optional): Minimum relevance (default: 0)

**Example**:
```
memory_search(query="How to fix mojibake encoding", limit=10)
```

**Common Issues**:
- Empty results: Try broader query or lower `min_score`
- Too many results: Increase `min_score` or add specific terms
- Slow queries: Consider using `memory_context` for task-aware search
```

**Implementation**: Stored in `docs/instructions/layers/L3/`, loaded via new MCP tool `load_instruction_layer`

---

### L4: Advanced/Experimental Layer (Optional)

**Purpose**: Cutting-edge features, experimental patterns, advanced optimization

**Characteristics**:
- Explicit opt-in only
- May contain unstable or experimental features
- For advanced users only
- Clearly marked as experimental

**Content**:
```markdown
## L4: Advanced Patterns (Experimental)

⚠️ **WARNING**: These patterns are experimental and may change.

### Hybrid Search Strategy

Combine semantic search with code inspection:

```python
# 1. Get semantic context
context = enrich_task_with_context(project_id="myproject", task="...")

# 2. Search for relevant components
components = search_project_knowledge(query="authentication flow")

# 3. Inspect code for implementation details
# (use code search tools)
```

**Performance Note**: This pattern adds ~200ms latency but improves accuracy by 15%.
```

**Implementation**: Stored in `docs/instructions/layers/L4/`, loaded via `load_instruction_layer` with `layer="L4"` parameter

---

## Layer Selection Logic

### Automatic Selection

```
┌─────────────────────────────────────────────────────────┐
│                    L0: Always Present                    │
│                  (Core Policy Block)                     │
└─────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────┐
│  L1: Task Summary (from handoff or task enrichment)     │
└─────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────┐
│         L2: Category-Specific (auto-loaded)             │
│  Based on: task_category OR inferred_domain OR tools    │
└─────────────────────────────────────────────────────────┘
                            │
                    ┌───────┴───────┐
                    │               │
                    ▼               ▼
            L3: On-Demand    L4: Opt-In Only
         (explicit request)  (advanced users)
```

### Category Detection

L2 category is determined by:

1. **Explicit Category** (from handoff packet):
   ```python
   category = handoff_packet.get("task_category")
   ```

2. **Inferred from Task Description**:
   ```python
   # Uses existing domain inference
   profile = await _post(api_base, "/skills/profile", {"text": task_desc})
   category = map_domain_to_category(profile.get("domains", []))
   ```

3. **Inferred from Tools Used**:
   ```python
   # From session context
   tools_called = session_context.get("tools_called", [])
   category = infer_category_from_tools(tools_called)
   ```

### Category Mapping

| Domain Pattern         | L2 Category          |
|------------------------|----------------------|
| memory, search, store  | `memory_operations`  |
| skill, marketplace     | `skills`             |
| coordination, message  | `coordination`       |
| law, governance, rule  | `governance`         |
| bootstrap, setup       | `project_bootstrap`  |
| handoff, task          | `handoff`            |

---

## MCP Tool Integration

### New Tools

#### `load_instruction_layer`

Load a specific instruction layer on demand.

**Input Schema**:
```json
{
  "type": "object",
  "properties": {
    "layer": {
      "type": "string",
      "enum": ["L3", "L4"],
      "description": "Layer to load (L3 for detailed reference, L4 for advanced)"
    },
    "category": {
      "type": "string",
      "description": "Category for L3 layer (e.g., 'memory_operations')"
    }
  }
}
```

**Response**: Returns the instruction layer content as markdown.

**Example**:
```python
load_instruction_layer(layer="L3", category="memory_operations")
```

#### `list_instruction_layers`

List available instruction layers.

**Input Schema**:
```json
{
  "type": "object",
  "properties": {
    "layer": {
      "type": "string",
      "enum": ["L2", "L3", "L4"],
      "description": "Layer to list (optional)"
    }
  }
}
```

**Response**: Returns list of available layers with descriptions.

### Modified Tools

#### `get_onboarding`

**Changes**:
- Always includes L0 policy block
- Includes L1 task summary if available
- Auto-loads appropriate L2 category layer
- Adds tip about `load_instruction_layer` for L3/L4

**Example Response**:
```markdown
## L0: Core Policy

You are an AI agent working through the SuperMemory MCP server...

## L1: Task Context

Current Task: Design an MCP-compatible layered instruction system...
Priority: high
Phase: task_framing

## L2: Governance Guidelines

When working with governance tools:

1. **Laws are Immutable**: Active project laws must be followed...
...

---
TIP: Call load_instruction_layer(layer="L3", category="governance") for detailed API reference.
```

#### `initialize` (MCP protocol)

**Changes**:
- Includes L0 policy in `_supermemory` hint
- Adds instruction layer metadata

**Example**:
```json
{
  "_supermemory": {
    "agent_id": "claude-code",
    "tip": "...",
    "semantic_defaults": [...],
    "instruction_layers": {
      "L0": "always_present",
      "L1": "from_handoff_or_enrichment",
      "L2": "auto_loaded: governance",
      "L3": "on_demand",
      "L4": "opt_in"
    }
  }
}
```

---

## Implementation Plan

### Phase 1: Core Infrastructure

1. **Create Instruction Layer Storage**
   - `app/services/instruction_layers.py` - Layer definitions and loading logic
   - `docs/instructions/layers/L2/` - Category-specific L2 layers
   - `docs/instructions/layers/L3/` - Detailed reference layers
   - `docs/instructions/layers/L4/` - Advanced/experimental layers

2. **Implement Layer Loading Logic**
   - `get_instruction_layer(layer, category)` - Retrieve layer content
   - `infer_instruction_category(task, tools, session)` - Auto-detect category
   - `build_l0_policy()` - Generate L0 block
   - `build_l1_summary(task)` - Generate L1 summary

3. **Add MCP Tools**
   - `load_instruction_layer` tool definition and handler
   - `list_instruction_layers` tool definition and handler

### Phase 2: Integration

4. **Modify `get_onboarding`**
   - Inject L0 policy
   - Add L1 summary from handoff context
   - Auto-load L2 category layer
   - Add tips for L3/L4

5. **Modify `initialize` Handler**
   - Include L0 in `_supermemory` hint
   - Add layer metadata

6. **Update `build_supermemory_initialize_hint`**
   - Include L0 policy in semantic_defaults

### Phase 3: Content

7. **Create L2 Category Layers**
   - `memory_operations.md`
   - `skills.md`
   - `coordination.md`
   - `governance.md`
   - `project_bootstrap.md`
   - `handoff.md`

8. **Create L3 Reference Layers**
   - Detailed API docs for each category
   - Code examples
   - Troubleshooting guides

9. **Create L4 Advanced Layer**
   - Experimental patterns
   - Performance optimization tips
   - Advanced workflows

---

## File Structure

```
docs/instructions/
├── LAYERED_INSTRUCTIONS_DESIGN.md    # This document
├── README.md                          # Quick start guide
└── layers/
    ├── L0/
    │   └── core_policy.md             # Immutable (code-generated)
    ├── L2/
    │   ├── memory_operations.md
    │   ├── skills.md
    │   ├── coordination.md
    │   ├── governance.md
    │   ├── project_bootstrap.md
    │   └── handoff.md
    ├── L3/
    │   ├── memory_operations/
    │   │   ├── api_reference.md
    │   │   ├── examples.md
    │   │   └── troubleshooting.md
    │   ├── skills/
    │   │   └── ...
    │   └── ...
    └── L4/
        ├── advanced_patterns.md
        └── experimental_features.md

app/services/
└── instruction_layers.py              # Layer loading logic
```

---

## Usage Examples

### Example 1: Memory Search Task

**Agent Request**: "Search for memories about mojibake encoding"

**Automatic Layers Loaded**:
```
L0: Core Policy (always)
L1: Task Summary (from request)
L2: memory_operations (auto-detected from "search" and "memories")
```

**Agent Sees**:
```markdown
## L0: Core Policy
You are an AI agent working through the SuperMemory MCP server...

## L1: Task Context
Current Task: Search for memories about mojibake encoding
Priority: normal

## L2: Memory Operations Guidelines
When working with memory tools:
1. Prefer Semantic Search: Use memory_search with natural language queries
...

---
TIP: Call load_instruction_layer(layer="L3", category="memory_operations") for detailed API reference.
```

### Example 2: Complex Governance Task

**Agent Request**: "Review and approve pending project laws"

**Automatic Layers Loaded**:
```
L0: Core Policy (always)
L1: Task Summary (from request)
L2: governance (auto-detected from "laws" and "approve")
```

**Agent Requests L3**:
```python
load_instruction_layer(layer="L3", category="governance")
```

**Agent Receives**: Detailed API documentation for governance tools, examples, and troubleshooting.

### Example 3: Advanced User Requests L4

**Agent Request**:
```python
load_instruction_layer(layer="L4")
```

**Agent Receives**: Advanced patterns, experimental features, with clear warning labels.

---

## Testing Strategy

1. **Unit Tests**
   - Layer loading logic
   - Category inference
   - L0/L1 generation

2. **Integration Tests**
   - `get_onboarding` includes correct layers
   - `load_instruction_layer` returns correct content
   - `initialize` includes L0 in hint

3. **End-to-End Tests**
   - Agent receives appropriate layers for different tasks
   - Layer selection works without manual intervention
   - Tool exposure is independent from instructions

---

## Migration Notes

### Existing Behavior

- `get_onboarding` currently returns a flat list of sections
- No explicit layering
- All content loaded at once

### New Behavior

- `get_onboarding` returns structured layers (L0, L1, L2)
- L3/L4 loaded on demand via new tools
- Clear separation between layers

### Backward Compatibility

- Existing `get_onboarding` calls continue to work
- New layer structure is additive, not breaking
- L0 content is derived from existing `build_supermemory_onboarding_basics()`

---

## Future Enhancements

1. **Dynamic Layer Generation**: Use LLM to generate task-specific L2 layers
2. **Layer Caching**: Cache frequently used L2/L3 layers for performance
3. **Layer Analytics**: Track which layers are most/least used
4. **Custom Layers**: Allow projects to define custom L2 layers
5. **Layer Versioning**: Support versioned instruction layers
6. **A/B Testing**: Test different layer compositions for effectiveness

---

## References

- [MCP Protocol Specification](https://modelcontextprotocol.io/)
- [SuperMemory Architecture](../PROJECT_KNOWLEDGE_MODEL.md)
- [Operational Instincts](../PROJECT_LAWS_SPEC.md)
