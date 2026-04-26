## L2: Memory Operations Guidelines

When working with memory tools:

1. **Prefer Semantic Search**: Use `memory_search` with natural language queries instead of exact matches
2. **Batch When Possible**: Use `memory_batch_store` for multiple memories instead of individual `memory_store` calls
3. **Context Matters**: Use `memory_context` for task-aware retrieval when you need project-specific context
4. **Track Sessions**: Provide `session_id` for related operations to enable better tracking and analytics
5. **Use Tree Structure**: For hierarchical knowledge, use `memory_tree_slice` to get context from general to specific

### Common Patterns

**Starting a Project Task**:
```python
# First, enrich task with project context
context = enrich_task_with_context(
    project_id="myproject",
    task="Implement feature X"
)

# Then search for relevant memories
memories = memory_search(
    query="feature X implementation patterns",
    limit=5
)
```

**Storing Multiple Memories**:
```python
# Use batch store for efficiency
memory_batch_store(memories=[
    {"content": "Memory 1", "agent_id": "agent-1"},
    {"content": "Memory 2", "agent_id": "agent-1"},
    {"content": "Memory 3", "agent_id": "agent-1"},
])
```

**Hierarchical Knowledge Retrieval**:
```python
# Get knowledge from general to specific
tree = memory_tree_slice(
    query="authentication system",
    agent_id="agent-1"
)
# Returns: general domain → principles → specific facts
```

### Tool Selection Guide

| Tool | Use When... |
|------|-------------|
| `memory_search` | Finding memories by semantic similarity |
| `memory_context` | Task-aware retrieval with project context |
| `memory_store` | Storing a single memory |
| `memory_batch_store` | Storing multiple memories efficiently |
| `memory_tree_slice` | Getting hierarchical knowledge context |
| `memory_cleanup` | Removing old/low-importance memories |
| `memory_stats` | Getting statistics about the memory collection |

### Performance Tips

- Set appropriate `limit` parameters (default 5 is usually sufficient)
- Use `min_score` to filter low-relevance results
- Batch operations when storing multiple memories
- Use `memory_context` for project tasks instead of manual context building
