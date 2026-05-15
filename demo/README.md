# Demo Dataset

This directory contains a safe synthetic dataset for public alpha demos.

Rules:
- no live user data
- no service telemetry copied from a real instance
- no secrets, local IPs, or machine-specific paths
- only small examples that demonstrate memory retrieval and project context patterns

Current asset:
- `demo_memories.jsonl` — small line-delimited memory records for local ingestion experiments

Example record shape:

```json
{"content":"The demo project uses a local-first memory server.","agent_id":"demo-user","memory_type":"fact","importance_score":0.7}
```

Recommended use:
- load into a fresh local instance
- verify `/api/v1/memories/search`
- use it for screenshots, docs, and public quickstart flows instead of any live store
