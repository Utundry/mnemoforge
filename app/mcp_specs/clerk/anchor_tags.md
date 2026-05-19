# Clerk Anchor Tags

Clerk exists to reduce data-maintenance burden. It should extract anchored evidence
from agent notes, preserve exact quotes, classify the capture type, and return a
review packet to the main agent before any governed mutation.

## Tags

Use paired tags around the smallest useful quote.

```text
<mcp-decision>We will keep executable workflow rules in JSON specs first.</mcp-decision>
<mcp-assumption>No YAML dependency is currently present in requirements.txt.</mcp-assumption>
<mcp-risk>Facade routing can misclassify improvement capture as checkpoint draft.</mcp-risk>
<mcp-next-step>Add a thin spec loader before runtime integration.</mcp-next-step>
<mcp-improvement>Refactor mcp_sse into data-driven FSM workflow execution.</mcp-improvement>
<mcp-rule-candidate>Verification commands must resolve the approved contour first.</mcp-rule-candidate>
```

## Review Packet Requirements

The Clerk review packet must include:

- `capture_type`
- `source_quotes`
- `proposed_payload`
- `mutation_tool`
- `confidence`
- `missing_evidence`
- `review_question`

The Clerk should not collapse unrelated captures into `record_task_checkpoint_args`
unless the selected `capture_type` is explicitly `task_checkpoint`.
