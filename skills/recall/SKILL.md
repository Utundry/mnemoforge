---
name: recall
description: Search Super Memory for previously saved information. Use when the user asks "do you remember", "what do you know about", "find in memory", "вспомни", "найди в памяти", or needs context from past sessions.
argument-hint: "[search query] [--agent <id>] [--type <type>] [--n <limit>]"
allowed-tools: mcp__super-memory__memory_search, mcp__super-memory__memory_health
---

# Recall — Search Super Memory

Search the semantic memory store for relevant saved information.

## Arguments

`$ARGUMENTS`

## Instructions

1. Parse the arguments:
   - **query**: Search text (required)
   - **agent**: Filter by agent ID (flag `--agent <id>`, default: no filter)
   - **type**: Filter by memory type (flag `--type <type>`, default: no filter)
   - **n**: Max results to return (flag `--n <n>`, default: 5)

2. Call `mcp__super-memory__memory_search` with:
   - `query`: the search text
   - `agent_id`: if provided
   - `memory_type`: if provided
   - `limit`: n value

3. Present results ranked by relevance score. For each result show:
   - Score and similarity
   - Memory type and category
   - Full content
   - Age (from timestamp)

4. If no results found, say so. If server is unreachable, suggest starting the services.

## Examples

```
/recall user preferences
/recall --agent project1 database configuration
/recall --type task --n 10 pending work
/recall what does the user prefer for code style
```
