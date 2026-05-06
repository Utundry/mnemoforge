---
name: remember
description: Save important information, facts, preferences, decisions or context to Super Memory for future recall. Use when the user says "remember that", "save this", "note that", "запомни", "сохрани в память", or when something clearly should be persisted for later.
argument-hint: "[content to remember] [--agent <id>] [--type fact|preference|experience|task|context] [--importance 0.0-1.0]"
allowed-tools: mcp__super-memory__memory_store, mcp__super-memory__memory_search, mcp__super-memory__memory_health
---

# Remember — Save to Super Memory

Save the provided information to the local semantic memory store.

## Arguments

`$ARGUMENTS`

## Instructions

1. Parse the arguments to extract:
   - **content**: The main text to remember (required — everything before any flags)
   - **agent**: Agent/user ID (flag `--agent <id>`, default: `"default"`)
   - **type**: Memory type (flag `--type <type>`, default: `"fact"`)
     - `fact` — factual information
     - `preference` — user preferences and habits
     - `experience` — things that happened
     - `task` — tasks and todos
     - `context` — project/session context
   - **importance**: Float 0.0–1.0 (flag `--importance <n>`, default: `0.7`)
   - **tags**: Optional comma-separated tags (flag `--tags <t1,t2>`)

2. If no flags are provided, treat the entire argument as content and use smart defaults:
   - Detect memory type from content: preferences → `preference`, tasks/todos → `task`, etc.
   - Set importance based on urgency words ("important", "critical", "always") → 0.9+

3. Call `mcp__super-memory__memory_store` with the parsed values.

4. Confirm success with the returned memory ID and a short summary.

## Examples

```
/remember User prefers Python over JavaScript
/remember --agent project1 --type preference --importance 0.9 Always use async/await, never callbacks
/remember --type task --importance 0.8 Review PR #42 before Friday
/remember --type context --tags docker,infra The production DB runs on port 5433 not 5432
```

## Output format

After saving, respond with:
```
Saved to memory: "<first 60 chars of content>"
ID: <uuid>
Type: <type> | Importance: <score>
```

If the memory server is unreachable, say so clearly and suggest running:
`docker compose up qdrant -d` and `uvicorn app.main:app` in `D:\work\mnemoforge`
