## L2: Governance Guidelines

When working with project governance:

1. **Laws are Immutable**: Active project laws MUST be followed - they represent agreed-upon rules
2. **Improvements Drive Progress**: Use improvements to report bugs, issues, and enhancement ideas
3. **Canonicals are Truth**: Canonical memories represent project knowledge that should not be duplicated
4. **Review Before Acting**: For governance operations, understand the impact before making changes
5. **Use Hierarchy**: Leverage the knowledge hierarchy (domain → principle → meta) for structured knowledge

### Common Patterns

**Listing Active Laws**:
```python
# Get active project laws
laws = list_project_laws(
    project="mnemoforge",
    status="active",
    limit=20
)

# Filter by scope if needed
domain_laws = list_project_laws(
    project="mnemoforge",
    status="active",
    scope="domain"
)
```

**Reporting an Issue**:
```python
# Report a bug or improvement idea
report_issue(
    title="Memory search returns irrelevant results",
    description="When searching for 'authentication', results include unrelated memories. Expected: only authentication-related memories.",
    project="mnemoforge",
    importance_score=0.8,
    tags=["bug", "search", "relevance"]
)
```

**Resolving an Improvement**:
```python
# Mark improvement as resolved after fixing
artifact_key = "improvement:mnemoforge:improvement-uuid-here"
resolve_artifact(
    artifact_key=artifact_key,
    acted_by="claude-code",
    action_source="inline_fix",
    reason="Fixed relevance scoring algorithm in memory_search"
)
```

**Working with Canonicals**:
```python
# Get knowledge hierarchy
hierarchy = knowledge_hierarchy(
    topic_prefix="authentication",
    limit_per_scope=25
)

# Get canonicals by scope
domain_canonicals = canonicals_by_scope(
    scope="domain",
    topic_prefix="auth"
)

# Suppress or reactivate a canonical
set_canonical_status(
    canonical_id="canonical-uuid-here",
    suppressed=True,
    reason="Outdated - replaced by newer version",
    reviewed_by="user"
)
```

### Tool Selection Guide

| Tool | Use When... |
|------|-------------|
| `list_project_laws` | Retrieving active project rules |
| `get_project_law` | Getting details of a specific law |
| `report_issue` | Reporting bugs or improvement ideas |
| `list_artifacts` | Primary search surface for improvements and tasks together |
| `get_artifact` | Getting details of a specific improvement or task |
| `resolve_artifact` | Marking an improvement as resolved or task as done |
| `reopen_artifact` | Reopening a resolved improvement or done task |
| `knowledge_hierarchy` | Inspecting structured knowledge |
| `canonicals_by_scope` | Listing canonicals by scope |
| `set_canonical_status` | Suppressing or reactivating canonicals |
| `merge_canonicals` | Merging duplicate canonicals |

### Law Lifecycle

```
observed → proposed → reviewed → user_confirmed → active → (suppressed/superseded/archived)
```

- **observed**: Pattern noticed by system
- **proposed**: Suggested as a potential law
- **reviewed**: Reviewed by governance process
- **user_confirmed**: Explicitly approved by user
- **active**: Currently enforced
- **suppressed**: Temporarily disabled
- **superseded**: Replaced by newer version
- **archived**: No longer relevant

### Best Practices

- Always check `list_project_laws` before making project-wide changes
- Use `report_issue` for any bugs or improvements you discover
- Prefer `list_artifacts(...)` over specialized task/improvement list endpoints because callers usually do not know the entity type in advance
- Use `list_artifacts(type="improvement")` only when you explicitly need to restrict results to improvements
- Use `created_after` / `created_before` / `updated_after` / `updated_before` on `list_artifacts(...)` for time-interval search
- Use `get_artifact(artifact_key)` to get details of a specific improvement or task
- Use `resolve_artifact(artifact_key, ...)` to mark improvements as resolved or tasks as done
- Provide clear `reason` when resolving artifacts or changing canonical status
- Use `knowledge_hierarchy` to understand project knowledge structure
- Respect the scope hierarchy: meta → principle → domain
