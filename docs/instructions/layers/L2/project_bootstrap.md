## L2: Project Bootstrap Guidelines

When working with external project bootstrap:

1. **Assess Readiness First**: Always call `get_project_readiness` before starting bootstrap to understand coverage
2. **Follow the Checklist**: Use `get_project_bootstrap_checklist` for step-by-step guidance
3. **Use Remote Snapshot**: For git-first projects, use `plan_remote_snapshot` and `sync_remote_snapshot`
4. **Choose Storage Mode**: Select appropriate storage mode (knowledge_only, selective_source_cache, full_mirror)
5. **Iterate**: Bootstrap is an iterative process - start with knowledge_only, expand as needed

### Common Patterns

**Assessing Project Readiness**:
```python
# Check if project is ready for external pilot
readiness = get_project_readiness(
    project_id="external-project"
)

# Review coverage and blockers
print(f"Knowledge coverage: {readiness['coverage']}")
print(f"Blockers: {readiness['blockers']}")
print(f"Next actions: {readiness['next_actions']}")
```

**Getting Bootstrap Checklist**:
```python
# Get ordered checklist for bootstrap
checklist = get_project_bootstrap_checklist(
    project_id="external-project"
)

# Follow steps in order
for step in checklist['steps']:
    print(f"{step['order']}. {step['title']}")
    print(f"   {step['description']}")
    # ... execute step ...
```

**Using Remote Snapshot (Git-First)**:
```python
# Plan the snapshot first
plan = plan_remote_snapshot(
    project_id="external-project",
    storage_mode="knowledge_only",
    snapshot={
        "source_mode": "git_snapshot",
        "repo": "https://github.com/user/repo.git",
        "branch": "main",
        "commit_sha": "abc123...",
        "dirty_workspace": False
    },
    changed_files=["app/main.py", "docs/README.md"],
    files=[
        {
            "path": "app/main.py",
            "status": "modified",
            "content": "...",
            "language": "python"
        }
    ]
)

# Review the plan
print(f"Action: {plan['action']}")
print(f"Storage mode: {plan['storage_mode']}")

# If ready, sync the snapshot
if plan['action'] == 'refreshed':
    result = sync_remote_snapshot(
        project_id="external-project",
        storage_mode="knowledge_only",
        snapshot=plan['snapshot'],
        changed_files=plan['changed_files'],
        files=plan['files']
    )
    print(f"Synced: {result['action']}")
```

### Storage Modes

| Mode | Description | Use When... |
|------|-------------|-------------|
| `knowledge_only` | Store only extracted knowledge | Initial bootstrap, minimal storage |
| `selective_source_cache` | Cache key source files | Need occasional code inspection |
| `full_mirror` | Full source mirror | Heavy code analysis required |

### Bootstrap Workflow

```
1. Assess readiness (get_project_readiness)
2. Get checklist (get_project_bootstrap_checklist)
3. Plan snapshot (plan_remote_snapshot)
4. Sync snapshot (sync_remote_snapshot)
5. Verify and iterate
```

### Tool Selection Guide

| Tool | Use When... |
|------|-------------|
| `get_project_readiness` | Assessing if project is ready for bootstrap |
| `get_project_bootstrap_checklist` | Getting step-by-step guidance |
| `plan_remote_snapshot` | Validating snapshot before ingest |
| `sync_remote_snapshot` | Ingesting/refreshing project knowledge |
| `get_storage_trust_status` | Checking storage health during bootstrap |

### Best Practices

- Start with `knowledge_only` storage mode for initial bootstrap
- Use `plan_remote_snapshot` to validate before `sync_remote_snapshot`
- Check `get_storage_trust_status` if you encounter issues
- Follow the checklist order for reliable bootstrap
- Expand storage mode only if needed (to save space)
