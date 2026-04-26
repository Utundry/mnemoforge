## L2: Skills Guidelines

When working with the skill marketplace:

1. **Search Before Creating**: Always use `skill_search` before creating a new skill to avoid duplicates
2. **Provide Context**: Include task/project context in `skill_search` for better filtering by domain
3. **Use Domain Tags**: Specify relevant domains when publishing skills for better discoverability
4. **Pin Critical Skills**: Use `pin_skill` for skills that should always appear in onboarding
5. **Record Outcomes**: Always call `record_outcome` at session end to improve future onboarding

### Common Patterns

**Finding Relevant Skills**:
```python
# Search with context for domain-aware results
skills = skill_search(
    context="Building a REST API with FastAPI",
    domains="web,api",
    limit=5
)
```

**Publishing a New Skill**:
```python
# Publish with domain tags for discoverability
skill_publish(
    name="fastapi-error-handling",
    content="# SKILL.md\n...",
    platform="claude",
    domain_tags=["web", "api", "fastapi"]
)
```

**Getting Onboarding**:
```python
# Call at session start for personalized guidance
onboarding = get_onboarding(
    agent_id="claude-code",
    task_description="Build a FastAPI REST API"
)

# Save pack_id for outcome recording
pack_id = onboarding.get("pack_id", "")

# ... do work ...

# Record outcome to improve future onboarding
record_outcome(
    agent_id="claude-code",
    pack_id=pack_id,
    success=True,
    skills_helpful=["skill-id-1", "skill-id-2"]
)
```

### Tool Selection Guide

| Tool | Use When... |
|------|-------------|
| `skill_search` | Finding relevant skills for your task |
| `skill_publish` | Publishing a new reusable skill |
| `skill_install` | Installing a skill by ID |
| `pin_skill` | Making a skill always appear in onboarding |
| `get_onboarding` | Getting personalized guidance at session start |
| `record_outcome` | Recording session outcome to improve future onboarding |

### Skill Content Best Practices

- Start with a clear, concise description
- Include concrete examples
- Specify domain tags for discoverability
- Keep it focused on a single, reusable pattern
- Document any prerequisites or dependencies
