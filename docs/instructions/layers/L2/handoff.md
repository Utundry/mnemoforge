## L2: Handoff Guidelines

When working with task handoffs:

1. **Use for Task Transfer**: Handoffs are for transferring tasks between agents, not for general coordination
2. **Define Scope Clearly**: Specify `write_scope` to define what files/areas the task may modify
3. **Set Definition of Done**: Clear `definition_of_done` prevents scope creep
4. **Use Appropriate Priority**: Set priority (high, medium, low) to indicate urgency
5. **Track Status**: Follow lifecycle: pending → picked_up → active → paused → closed/archived

### Common Patterns

**Creating a Handoff**:
```python
# Create a task handoff for another agent
handoff_task(
    from_agent="claude-code",
    to_agent="codex",
    task_description="Implement the layered instruction system in mcp_sse.py",
    write_scope=["app/routers/mcp_sse.py", "app/services/instruction_layers.py"],
    phase="implementation",
    priority="high",
    definition_of_done="L0-L4 layers are implemented and documented",
    expected_output_shape="Modified files with new instruction layer logic"
)
```

**Picking Up a Handoff**:
```python
# Check for handoffs addressed to you
handoffs = pickup_handoff(
    agent_id="codex"
)

# Process each handoff
for h in handoffs:
    print(f"Task: {h['task_description']}")
    print(f"Write scope: {h['write_scope']}")
    print(f"Definition of done: {h['definition_of_done']}")
    # ... work on task ...
```

**Updating Handoff Status**:
```python
# Mark as active when starting work
update_handoff_status(
    memory_id="handoff-uuid-here",
    status="active",
    acted_by="codex",
    reason="Starting implementation"
)

# ... do work ...

# Mark as closed when done
update_handoff_status(
    memory_id="handoff-uuid-here",
    status="closed",
    acted_by="codex",
    result_summary="Implemented L0-L4 layers with auto-loading",
    verification_summary="All tests pass, documentation complete"
)
```

**Resuming a Paused Handoff**:
```python
# Resume a paused or picked-up task
resume_handoff(
    memory_id="handoff-uuid-here",
    refresh_context=True,
    reason="Resuming after interruption"
)
```

### Handoff Lifecycle

```
pending → picked_up → active → (paused) → closed/archived
```

- **pending**: Task created, waiting for pickup
- **picked_up**: Agent has received the task
- **active**: Agent is working on the task
- **paused**: Task temporarily suspended
- **closed**: Task completed
- **archived**: Task no longer relevant

### Execution Modes

| Mode | Description | Use When... |
|------|-------------|-------------|
| `max_quality` | Best possible result, higher cost | Critical tasks, production code |
| `balanced` | Good quality, reasonable cost | Most tasks |
| `economy` | Acceptable quality, lower cost | Non-critical tasks |
| `strict_economy` | Minimum viable, lowest cost | Prototyping, exploration |

### Tool Selection Guide

| Tool | Use When... |
|------|-------------|
| `handoff_task` | Creating a new task handoff |
| `pickup_handoff` | Retrieving tasks addressed to you |
| `list_handoffs` | Viewing task history or status |
| `update_handoff_status` | Updating task lifecycle |
| `resume_handoff` | Resuming a paused task |
| `expand_handoff_refs` | Getting detailed project context from handoff |

### Best Practices

- Always specify `write_scope` to bound what the task may modify
- Provide clear `definition_of_done` to prevent scope creep
- Set appropriate `priority` and `execution_mode`
- Update status as you progress through the task
- Use `expand_handoff_refs` if you need more context than the summary
- Set `result_summary` and `verification_summary` when closing
