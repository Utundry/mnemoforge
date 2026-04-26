## L2: Coordination Guidelines

When working with agent coordination:

1. **Use for Operational Messages**: Coordination messages are for requests, replies, and status updates - they do NOT become project truth automatically
2. **Pick Up Messages**: Call `pickup_coordination_messages` at session start to check for messages addressed to you
3. **Specify Project Scope**: Always include `project` parameter for project-scoped coordination
4. **Set Appropriate Priority**: Use priority levels (low, normal, high, urgent) to indicate urgency
5. **Update Status**: Follow the lifecycle: new → acknowledged → in_progress → answered/closed

### Common Patterns

**Starting a Session**:
```python
# Check for incoming messages
messages = pickup_coordination_messages(
    agent_id="claude-code",
    project="supermemory",
    limit=10
)

# Process each message
for msg in messages:
    print(f"From: {msg['from_agent']}")
    print(f"Content: {msg['content']}")
    # ... handle message ...
```

**Sending a Request**:
```python
# Send a request to another agent
send_coordination_message(
    project="supermemory",
    from_agent="claude-code",
    to_agent="codex",
    content="Please review the changes in app/services/mcp_tool_contracts.py",
    message_type="request_action",
    priority="normal",
    requested_action="review_code"
)
```

**Replying to a Message**:
```python
# Reply to a specific message
send_coordination_message(
    project="supermemory",
    from_agent="claude-code",
    to_agent="codex",
    content="I've reviewed the changes and found no issues.",
    message_type="response",
    response_to_message_id="message-uuid-here",
    priority="normal"
)
```

**Updating Message Status**:
```python
# Mark message as in progress
update_coordination_message_status(
    message_id="message-uuid-here",
    status="in_progress",
    acted_by="claude-code",
    action_source="coordination",
    reason="Starting work on the request"
)

# ... do work ...

# Mark as answered when done
update_coordination_message_status(
    message_id="message-uuid-here",
    status="answered",
    acted_by="claude-code",
    action_source="coordination",
    reason="Request completed successfully"
)
```

### Message Lifecycle

```
new → acknowledged → in_progress → answered/closed
```

- **new**: Message created, not yet picked up
- **acknowledged**: Agent has received the message
- **in_progress**: Agent is working on the request
- **answered**: Agent has responded
- **closed**: Conversation is complete

### Tool Selection Guide

| Tool | Use When... |
|------|-------------|
| `send_coordination_message` | Sending a message to another agent |
| `pickup_coordination_messages` | Checking for new messages at session start |
| `list_coordination_messages` | Viewing conversation history or inbox |
| `update_coordination_message_status` | Updating message lifecycle status |

### Best Practices

- Always include `project` for project-scoped coordination
- Use `response_to_message_id` when replying to maintain thread context
- Set appropriate `priority` based on urgency
- Update message status as you progress through the request
- Use `message_type` to clarify intent (question, request_action, response, etc.)
