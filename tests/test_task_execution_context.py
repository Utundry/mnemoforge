import pytest

PREFIX = "/api/v1"


@pytest.mark.asyncio
async def test_task_execution_context_for_server_verification_returns_scoped_rules_and_tools(client):
    for payload in (
        {
            "project": "alpha",
            "title": "Docker Test Contour",
            "statement": "Agents must run pytest through the dedicated Docker test contour, not host pytest.",
            "rationale": "Prevents Windows permission failures and production database contamination.",
            "agent_id": "codex",
            "scope": "project",
            "status": "active",
            "confirmed_by": "user",
            "tags": ["docker", "pytest", "testing-contour"],
        },
        {
            "project": "alpha",
            "title": "Dev Server Restart Is Agent-Managed",
            "statement": "Agents may restart memory-server-dev for live validation of server-side changes.",
            "rationale": "Live server restart is separate from pytest verification.",
            "agent_id": "codex",
            "scope": "project",
            "status": "active",
            "confirmed_by": "user",
            "tags": ["docker", "live-validation"],
        },
        {
            "project": "alpha",
            "title": "Agent Internal Text Uses English",
            "statement": "Agent-facing internal text should be written in English.",
            "rationale": "Improves portability and usually saves tokens.",
            "agent_id": "codex",
            "scope": "principle",
            "status": "active",
            "confirmed_by": "user",
            "tags": ["language", "agent-rules"],
        },
    ):
        created = await client.post(f"{PREFIX}/laws", json=payload)
        assert created.status_code == 201, created.text

    response = await client.post(
        f"{PREFIX}/task-execution-context",
        json={
            "project": "alpha",
            "task": "Verify a server-side API change with pytest before live validation.",
            "state": "verification",
            "changed_files": ["app/routers/example.py"],
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()

    required_titles = {item["title"] for item in data["required_rules"]}
    recommended_titles = {item["title"] for item in data["recommended_rules"]}
    assert "Docker Test Contour" in required_titles
    assert "Agent Internal Text Uses English" in required_titles
    assert "Dev Server Restart Is Agent-Managed" in recommended_titles
    assert any("host pytest" in item for item in data["risk_controls"])
    assert any("docker" in item.casefold() and "contour" in item.casefold() for item in data["risk_controls"])
    assert data["readiness"]["ready_to_enter"] is True
    assert data["operation_tray"]["state"] == "verification"
    assert "tool_recommend" in data["operation_tray"]["primary_tools"]
    assert data["operation_tray"]["bureaucracy_budget"]["mode"] == "lightweight"
    assert data["operation_tray"]["bureaucracy_budget"]["stage_evidence_format"] == "checkpoint:<change_id>"
    assert any("Docker-based verification contour" in item for item in data["operation_tray"]["risk_controls"])
    assert data["next_transitions"] == ["live_validation", "checkpointing", "implementation"]
    assert data["recommended_tools"][0]["family"] == "testing"


@pytest.mark.asyncio
async def test_task_execution_context_can_return_policy_without_rules_or_tools(client):
    response = await client.post(
        f"{PREFIX}/task-execution-context",
        json={
            "project": "alpha",
            "task": "Prepare a handoff.",
            "state": "handoff",
            "include_rules": False,
            "include_tools": False,
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["required_rules"] == []
    assert data["recommended_tools"] == []
    assert data["operation_tray"] is None
    assert data["expected_outputs"]


@pytest.mark.asyncio
async def test_task_execution_context_checkpointing_surfaces_rule_candidate_workflow(client):
    created = await client.post(f"{PREFIX}/laws", json={
        "project": "alpha",
        "title": "Self-Improving Project Laws Are A Core Goal",
        "statement": "Agents should capture useful rule candidates during task work and review them after closeout.",
        "rationale": "Improves governance without manually mining whole dialogues.",
        "agent_id": "codex",
        "scope": "project",
        "status": "active",
        "confirmed_by": "user",
        "tags": ["rule", "candidate", "checkpoint"],
    })
    assert created.status_code == 201, created.text

    response = await client.post(
        f"{PREFIX}/task-execution-context",
        json={
            "project": "alpha",
            "task": "Checkpoint a task that discovered a reusable project rule.",
            "state": "checkpointing",
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    tool_names = {tool for item in data["recommended_tools"] for tool in item["tools"]}
    assert "record_stenographer_span" in tool_names
    assert "project_rule_candidates_from_stenography" in tool_names
    assert "list_rule_candidates" in tool_names
    assert data["operation_tray"]["primary_tools"][:3] == [
        "clerk_draft_report",
        "draft_checkpoint_from_spans",
        "record_task_checkpoint",
    ]
    assert "clerk_draft_report" in data["operation_tray"]["assistant_tools"]
    assert any("rule marker" in item for item in data["risk_controls"])
    assert any(item["title"] == "Self-Improving Project Laws Are A Core Goal" for item in data["required_rules"])


@pytest.mark.asyncio
async def test_task_execution_context_documentation_surfaces_structured_knowledge_projection(client):
    response = await client.post(
        f"{PREFIX}/task-execution-context",
        json={
            "project": "alpha",
            "task": "Record architecture documentation in the knowledge tree before updating README.",
            "state": "documentation",
            "stage_evidence": ["checkpoint:implementation-1"],
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    tool_names = {tool for item in data["recommended_tools"] for tool in item["tools"]}
    assert "upsert_knowledge_tree_node" in tool_names
    assert "record_task_checkpoint" in tool_names
    assert data["operation_tray"]["state"] == "documentation"
    assert data["operation_tray"]["bureaucracy_budget"]["mode"] == "lightweight"
    assert data["operation_tray"]["bureaucracy_budget"]["required_records"] == ["structured_knowledge_update"]
    assert any("Markdown" in item and "projection" in item for item in data["risk_controls"])


@pytest.mark.asyncio
async def test_task_execution_context_blocks_implementation_until_framing_is_recorded(client):
    response = await client.post(
        f"{PREFIX}/task-execution-context",
        json={
            "project": "alpha",
            "task": "Implement Operation Tray readiness gate.",
            "state": "implementation",
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["readiness"]["ready_to_enter"] is False
    assert "task_framing_not_recorded" in data["readiness"]["missing_prerequisites"]
    assert data["operation_tray"]["primary_tools"] == ["record_task_checkpoint"]
    assert data["operation_tray"]["bureaucracy_budget"]["required_records"] == ["framing_checkpoint"]
    assert "draft_task_checkpoint" in data["operation_tray"]["assistant_tools"]


@pytest.mark.asyncio
async def test_task_execution_context_allows_implementation_with_stage_evidence(client):
    response = await client.post(
        f"{PREFIX}/task-execution-context",
        json={
            "project": "alpha",
            "task": "Implement Operation Tray readiness gate.",
            "state": "implementation",
            "stage_evidence": ["checkpoint:framing-1"],
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["readiness"]["ready_to_enter"] is True
    assert data["readiness"]["evidence"] == ["checkpoint:framing-1"]
    assert data["operation_tray"]["primary_tools"] != ["record_task_checkpoint"]


@pytest.mark.asyncio
async def test_task_execution_context_uses_project_local_testing_rule_from_tree(client):
    parent = await client.post(
        f"{PREFIX}/tree/upsert-by-path",
        json={
            "topic_path": "alpha/testing",
            "title": "Alpha testing",
            "type": "area",
            "status": "active",
        },
    )
    assert parent.status_code == 200, parent.text

    child = await client.post(
        f"{PREFIX}/tree/upsert-by-path",
        json={
            "topic_path": "alpha/testing/docker-test-contour",
            "parent_topic_path": "alpha/testing",
            "title": "Alpha Docker test contour",
            "type": "leaf",
            "status": "active",
            "goal": "Run Alpha verification in its Docker test contour.",
            "structured_fields": {
                "rule_kind": "project_local_testing_rule",
                "applies_to_project": "alpha",
                "required_runner": "scripts/run_pytest_docker.ps1",
                "runner_service": "alpha-test-runner",
                "forbidden_default": "host pytest for Alpha verification",
            },
            "responsibility": "Use the Docker test contour, not host pytest, for Alpha verification.",
            "tags": ["project-local", "testing-contour", "docker", "pytest"],
        },
    )
    assert child.status_code == 200, child.text

    response = await client.post(
        f"{PREFIX}/task-execution-context",
        json={
            "project": "alpha",
            "task": "Verify the server change with pytest.",
            "state": "verification",
            "changed_files": ["app/routers/example.py"],
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()

    assert data["readiness"]["ready_to_enter"] is True
    assert "verification_contour_unknown" not in data["readiness"]["missing_prerequisites"]
    assert any(item["title"] == "Alpha Docker test contour" for item in data["required_rules"])
    assert any("project knowledge" in item for item in data["risk_controls"])
    assert any("host pytest" in item for item in data["risk_controls"])


@pytest.mark.asyncio
async def test_task_execution_context_carries_project_testing_rule_outside_verification(client):
    parent = await client.post(
        f"{PREFIX}/tree/upsert-by-path",
        json={
            "topic_path": "beta/testing",
            "title": "Beta testing",
            "type": "area",
            "status": "active",
        },
    )
    assert parent.status_code == 200, parent.text

    child = await client.post(
        f"{PREFIX}/tree/upsert-by-path",
        json={
            "topic_path": "beta/testing/docker-test-contour",
            "parent_topic_path": "beta/testing",
            "title": "Beta Docker test contour",
            "type": "leaf",
            "status": "active",
            "goal": "Run Beta verification in its Docker test contour.",
            "structured_fields": {
                "rule_kind": "project_local_testing_rule",
                "applies_to_project": "beta",
                "required_runner": "scripts/run_pytest_docker.ps1",
                "forbidden_default": "host pytest for Beta verification",
            },
            "responsibility": "Use the Docker test contour, not host pytest, for Beta verification.",
            "tags": ["project-local", "testing-contour", "docker", "pytest"],
        },
    )
    assert child.status_code == 200, child.text

    response = await client.post(
        f"{PREFIX}/task-execution-context",
        json={
            "project": "beta",
            "task": "Review why a new agent session missed project testing rules.",
            "state": "operator_review",
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()

    assert not any(item["title"] == "Beta Docker test contour" for item in data["required_rules"])
    assert any(item["title"] == "Beta Docker test contour" for item in data["recommended_rules"])


@pytest.mark.asyncio
async def test_task_execution_context_treats_user_confirmed_restart_window_as_effective(client):
    created = await client.post(
        f"{PREFIX}/laws",
        json={
            "project": "alpha",
            "title": "Wait for runtime owner stale window after Docker restart",
            "statement": (
                "After restarting the MnemoForge Docker server, agents must allow for the runtime owner "
                "stale window. If health checks fail immediately after restart and logs show "
                "RuntimeOwnershipError for qdrant_data, wait up to MNEMOFORGE_RUNTIME_OWNER_STALE_SECONDS, "
                "default 120 seconds, then retry health and MCP checks."
            ),
            "rationale": "The runtime owner guard protects qdrant_data from concurrent writers.",
            "agent_id": "codex",
            "scope": "project",
            "status": "user_confirmed",
            "confirmed_by": "user",
            "tags": ["docker", "restart", "runtime-owner"],
        },
    )
    assert created.status_code == 201, created.text

    response = await client.post(
        f"{PREFIX}/task-execution-context",
        json={
            "project": "alpha",
            "task": "Restart the Docker server and validate MCP after the restart.",
            "state": "live_validation",
            "prior_stage_recorded": True,
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()

    assert data["coverage"]["active_laws_seen"] == 1
    assert any(
        item["title"] == "Wait for runtime owner stale window after Docker restart"
        for item in data["recommended_rules"]
    )
    assert any("stale window" in item for item in data["risk_controls"])


@pytest.mark.asyncio
async def test_task_execution_context_verification_is_project_aware_when_contour_is_unknown(client):
    response = await client.post(
        f"{PREFIX}/task-execution-context",
        json={
            "project": "non_docker_project",
            "task": "Verify the parser change.",
            "state": "verification",
            "changed_files": ["src/parser.py"],
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["readiness"]["ready_to_enter"] is False
    assert "verification_contour_unknown" in data["readiness"]["missing_prerequisites"]
    assert any("Verification contour unknown" in item for item in data["risk_controls"])
    assert not any("Use the dedicated Docker test contour" in item for item in data["risk_controls"])
