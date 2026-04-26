import io
import json
import sys

from mcp import server as mcp_stdio


def _run_stdio_messages(messages: list[dict]) -> list[dict]:
    stdin = io.StringIO("\n".join(json.dumps(message) for message in messages) + "\n")
    stdout = io.StringIO()

    original_stdin = sys.stdin
    original_stdout = sys.stdout
    try:
        sys.stdin = stdin
        sys.stdout = stdout
        mcp_stdio.main()
    finally:
        sys.stdin = original_stdin
        sys.stdout = original_stdout

    responses: list[dict] = []
    for line in stdout.getvalue().splitlines():
        line = line.strip()
        if line:
            responses.append(json.loads(line))
    return responses


def test_stdio_tools_list_includes_handoff_packet_contract():
    responses = _run_stdio_messages(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ]
    )

    tools_list = next(response["result"]["tools"] for response in responses if response.get("id") == 2)
    tools_by_name = {tool["name"]: tool for tool in tools_list}

    assert "handoff_task" in tools_by_name
    assert "resume_handoff" in tools_by_name
    assert "decompose_task_packet" in tools_by_name
    assert "create_task_packets" in tools_by_name
    assert "route_task_packet_execution" in tools_by_name
    assert "dispatch_background_task_packet" in tools_by_name
    assert "reconcile_background_task_packet" in tools_by_name

    handoff_schema = tools_by_name["handoff_task"]["inputSchema"]["properties"]
    assert "owner_agent" in handoff_schema
    assert "write_scope" in handoff_schema
    assert handoff_schema["write_scope"]["type"] == "array"

    update_schema = tools_by_name["update_handoff_status"]["inputSchema"]["properties"]
    assert "executor_used" in update_schema
    assert "model_used" in update_schema

    decompose_schema = tools_by_name["decompose_task_packet"]["inputSchema"]["properties"]
    assert "write_scope" in decompose_schema
    assert "max_packets" in decompose_schema
    assert "execution_mode" in decompose_schema

    create_schema = tools_by_name["create_task_packets"]["inputSchema"]["properties"]
    assert create_schema["agent_id"]["default"] == "handoff"
    assert "execution_mode" in create_schema
    assert "packets" in create_schema
    packet_schema = create_schema["packets"]["items"]["properties"]
    assert "why_now" in packet_schema
    assert "core_instinct_ids" in packet_schema
    assert "supporting_instinct_ids" in packet_schema
    assert "execution_mode" in packet_schema
    assert "background_job_type" in packet_schema
    assert "background_payload" in packet_schema
    assert "suggested_execution_tier" in packet_schema
    assert "model_hint" in packet_schema

    route_schema = tools_by_name["route_task_packet_execution"]["inputSchema"]["properties"]
    assert "memory_id" in route_schema
    assert "packet" in route_schema
    assert route_schema["packet"]["additionalProperties"] is True
    packet_route_schema = route_schema["packet"]["properties"]
    assert "task_description" in packet_route_schema
    assert "write_scope" in packet_route_schema
    assert "execution_mode" in packet_route_schema
    assert "background_job_type" in packet_route_schema
    assert "background_payload" in packet_route_schema
    assert "suggested_execution_tier" in packet_route_schema
    assert "model_hint" in packet_route_schema

    dispatch_schema = tools_by_name["dispatch_background_task_packet"]["inputSchema"]["properties"]
    assert "memory_id" in dispatch_schema
    assert "acted_by" in dispatch_schema
    assert "reason" in dispatch_schema


def test_stdio_handoff_workspace_summary_surfaces_background_job_state():
    result = mcp_stdio._format_handoff_workspace_summary(
        {
            "agent_id": "codex",
            "statuses": ["closed"],
            "total": 1,
            "recent_packets": [
                {
                    "task_id": "task-1",
                    "handoff_label": "resume42",
                    "status": "closed",
                    "owner_agent": "codex",
                    "phase": "task_framing",
                    "priority": "high",
                    "memory_id": "mem-1",
                    "background_job_status": "done",
                    "dispatched_job_id": "job-docs-1",
                }
            ],
        }
    )

    assert "background_job_status=done" in result
    assert "dispatched_job_id=job-docs-1" in result
