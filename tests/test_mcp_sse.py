import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from app import dependencies
from app.main import _should_suppress_asyncio_transport_error
from app.routers import mcp_sse
from app.services.mcp_mailbox_actions import MailboxActionDependencies, mailbox_finish_task, mailbox_start_task
from app.services.mcp_simple_read_actions import explicit_artifact_list_type
from mcp import server as mcp_stdio


def _install_task_claim_store(monkeypatch, *, project: str = "alpha", task_id: str = "task-1", agent_id: str = "codex", session_id: str = "sess-1"):
    from pathlib import Path
    from app.services import task_lease_service as lease_mod

    store = lease_mod.TaskLeaseStore(Path(":memory:"))
    monkeypatch.setattr(lease_mod, "_STORE", store)
    result = store.claim(project=project, task_id=task_id, owner_agent=agent_id, session_id=session_id)
    return store, result.work_token


class _FakeHandle:
    def __repr__(self) -> str:
        return "<Handle _ProactorBasePipeTransport._call_connection_lost(None)>"


class TestMcpDiscovery:
    async def test_oauth_discovery_root_is_benign(self, client):
        r = await client.get("/.well-known/oauth-authorization-server")
        assert r.status_code == 404
        body = r.json()
        assert body["detail"] == "OAuth not supported"

    async def test_oauth_discovery_variants_are_benign(self, client):
        for path in (
            "/.well-known/oauth-authorization-server/mcp/sse",
            "/mcp/sse/.well-known/oauth-authorization-server",
        ):
            r = await client.get(path)
            assert r.status_code == 404
            assert r.json()["detail"] == "OAuth not supported"


class TestMcpToolExecution:
    async def test_tools_list_defaults_to_compact_thematic_catalog(self):
        response = await mcp_sse._handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            "http://test",
        )

        result = response["result"]
        names = [tool["name"] for tool in result["tools"]]
        assert names[:6] == ["help", "state", "get", "submit", "mailbox_state", "mailbox_submit"]
        assert len(names) <= 12
        assert len(names) < len(mcp_sse.TOOLS)
        assert "report_issue" not in names
        assert "record_work_result" not in names
        assert "get_task_execution_context" not in names
        assert result["_mnemoforge"]["catalog_mode"] == "compact"
        assert result["_mnemoforge"]["full_catalog_request"] == {"method": "tools/list", "params": {"mode": "full"}}
        assert result["_mnemoforge"]["recommended_first_tool"] == "help"
        assert "help/state/get/submit" in result["_mnemoforge"]["reason"]

    async def test_tools_list_full_mode_returns_full_catalog(self):
        response = await mcp_sse._handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"mode": "full"}},
            "http://test",
        )

        tools = response["result"]["tools"]
        names = [tool["name"] for tool in tools]
        assert len(tools) == len(mcp_sse.TOOLS)
        assert "report_issue" in names
        by_name = {tool["name"]: tool for tool in tools}
        assert response["result"]["_mnemoforge"]["catalog_mode"] == "full"
        assert response["result"]["_mnemoforge"]["recommended_public_surface"] == ["help", "state", "get", "submit"]
        assert "Do not use it as the starting workflow" in response["result"]["_mnemoforge"]["warning"]
        assert by_name["help"]["_mnemoforge"]["surface_role"] == "public_entrypoint"
        assert by_name["help"]["annotations"]["mnemoforge_recommended_start"] is True
        assert by_name["mailbox_state"]["_mnemoforge"]["surface_role"] == "compatibility_legacy"
        assert by_name["memory_store"]["_mnemoforge"]["surface_role"] == "specialized_fallback"
        assert by_name["memory_store"]["description"].startswith("[Specialized fallback]")

    async def test_memory_tools_expose_project_scope_fields(self):
        response = await mcp_sse._handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"mode": "full"}},
            "http://test",
        )

        tools = {tool["name"]: tool for tool in response["result"]["tools"]}
        store_props = tools["memory_store"]["inputSchema"]["properties"]
        search_props = tools["memory_search"]["inputSchema"]["properties"]

        assert "project" in store_props
        assert "project_id" in store_props
        assert "project" in search_props
        assert "project_id" in search_props
        assert "context_project" in search_props

    async def test_tools_list_env_can_restore_full_default(self, monkeypatch):
        monkeypatch.setenv("MCP_TOOL_CATALOG_DEFAULT", "full")
        response = await mcp_sse._handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            "http://test",
        )

        assert len(response["result"]["tools"]) == len(mcp_sse.TOOLS)
        assert response["result"]["_mnemoforge"]["catalog_mode"] == "full"

    async def test_tools_list_compact_catalog_surfaces_simple_mailbox_first(self):
        response = await mcp_sse._handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"mode": "compact", "limit": 4}},
            "http://test",
        )

        result = response["result"]
        names = [tool["name"] for tool in result["tools"]]
        assert names == ["help", "state", "get", "submit"]
        assert len(names) == 4
        assert len(names) < len(mcp_sse.TOOLS)
        assert result["_mnemoforge"]["catalog_mode"] == "compact"
        assert result["_mnemoforge"]["schema_mode"] == "summary"
        assert result["_mnemoforge"]["recommended_first_tool"] == "help"
        assert result["_mnemoforge"]["full_catalog_available"] is True
        assert "inputSummary" in result["tools"][0]
        assert result["tools"][0]["inputSchema"]["type"] == "object"

    async def test_simple_tool_build_info_is_diagnostic_only(self, monkeypatch):
        monkeypatch.setenv("MNEMOFORGE_GIT_COMMIT", "abc1234")
        monkeypatch.setenv("MNEMOFORGE_BUILD_TAG", "test-tag")
        monkeypatch.delenv("MNEMOFORGE_EXPOSE_BUILD_INFO", raising=False)

        normal = json.loads(await mcp_sse._execute_tool("help", {"project": "alpha"}, "http://test"))
        assert "server_build" not in normal

        diagnostic = json.loads(
            await mcp_sse._execute_tool("help", {"project": "alpha", "diagnostic": True}, "http://test")
        )
        assert diagnostic["server_build"]["service"] == "mnemoforge"
        assert diagnostic["server_build"]["git_commit"] == "abc1234"
        assert diagnostic["server_build"]["build_tag"] == "test-tag"

    async def test_simple_tool_build_info_can_be_enabled_by_env(self, monkeypatch):
        monkeypatch.setenv("MNEMOFORGE_GIT_COMMIT", "def5678")
        monkeypatch.setenv("MNEMOFORGE_EXPOSE_BUILD_INFO", "true")

        data = json.loads(await mcp_sse._execute_tool("state", {"project": "alpha"}, "http://test"))
        assert data["server_build"]["git_commit"] == "def5678"

    async def test_tools_list_compact_can_request_full_schemas(self):
        response = await mcp_sse._handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {"mode": "compact", "limit": 2, "schema_mode": "full"},
            },
            "http://test",
        )

        result = response["result"]
        assert result["_mnemoforge"]["catalog_mode"] == "compact"
        assert result["_mnemoforge"]["schema_mode"] == "full"
        assert "inputSchema" in result["tools"][0]
        assert "inputSummary" not in result["tools"][0]

    async def test_initialize_can_negotiate_compact_tools_list_default(self):
        session_id = "sess-compact-tools-list"
        initialized = await mcp_sse._handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "Codex CLI"},
                    "_mnemoforge": {"tool_catalog": {"preferred_mode": "compact"}},
                },
            },
            "http://test",
            session_id=session_id,
        )
        assert initialized["result"]["_mnemoforge"]["tool_catalog"]["negotiated_mode"] == "compact"

        response = await mcp_sse._handle(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            "http://test",
            session_id=session_id,
        )
        result = response["result"]
        names = [tool["name"] for tool in result["tools"]]
        assert names[0] == "help"
        assert result["_mnemoforge"]["catalog_mode"] == "compact"
        assert result["_mnemoforge"]["schema_mode"] == "summary"
        assert result["_mnemoforge"]["recommended_first_tool"] == "help"
        assert "inputSummary" in result["tools"][0]
        assert result["tools"][0]["inputSchema"]["type"] == "object"
        assert len(names) < len(mcp_sse.TOOLS)

        full = await mcp_sse._handle(
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {"mode": "full"}},
            "http://test",
            session_id=session_id,
        )
        assert len(full["result"]["tools"]) == len(mcp_sse.TOOLS)
        assert full["result"]["_mnemoforge"]["catalog_mode"] == "full"

    async def test_initialize_can_negotiate_compact_tools_list_via_capabilities(self):
        session_id = "sess-compact-tools-list-capabilities"
        initialized = await mcp_sse._handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "Codex CLI"},
                    "capabilities": {
                        "experimental": {
                            "mnemoforge": {
                                "tool_catalog_mode": "compact",
                            }
                        }
                    },
                },
            },
            "http://test",
            session_id=session_id,
        )
        assert initialized["result"]["_mnemoforge"]["tool_catalog"]["negotiated_mode"] == "compact"

        response = await mcp_sse._handle(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            "http://test",
            session_id=session_id,
        )
        assert response["result"]["_mnemoforge"]["catalog_mode"] == "compact"

    async def test_initialize_can_negotiate_small_context_hygiene(self):
        session_id = "sess-small-context-hygiene"
        initialized = await mcp_sse._handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "Small Model Agent"},
                    "_mnemoforge": {
                        "tool_catalog": {"preferred_mode": "compact"},
                        "context": {"hygiene_mode": "small_context"},
                    },
                },
            },
            "http://test",
            session_id=session_id,
        )

        assert initialized["result"]["_mnemoforge"]["context_hygiene"]["negotiated_mode"] == "small_context"

    async def test_initialize_infers_small_context_from_model_window(self):
        session_id = "sess-small-context-window"
        initialized = await mcp_sse._handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "Small Window Agent"},
                    "modelInfo": {"name": "local-dev-model", "contextWindow": 32000},
                },
            },
            "http://test",
            session_id=session_id,
        )

        info = initialized["result"]["_mnemoforge"]
        assert info["tool_catalog"]["negotiated_mode"] == "compact"
        assert info["tool_catalog"]["inferred"] is True
        assert info["context_hygiene"]["negotiated_mode"] == "small_context"
        assert info["context_hygiene"]["inferred"] is True
        assert info["context_hygiene"]["model_context_window"] == 32000

        listed = await mcp_sse._handle(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            "http://test",
            session_id=session_id,
        )
        assert listed["result"]["_mnemoforge"]["catalog_mode"] == "compact"
        assert listed["result"]["_mnemoforge"]["schema_mode"] == "summary"
        assert "inputSummary" in listed["result"]["tools"][0]
        assert listed["result"]["tools"][0]["inputSchema"]["type"] == "object"

    async def test_initialize_infers_small_context_from_model_profile(self):
        initialized = await mcp_sse._handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "Local SLM Agent"},
                    "agent_profile": "local-slm",
                },
            },
            "http://test",
            session_id="sess-small-context-profile",
        )

        info = initialized["result"]["_mnemoforge"]
        assert info["tool_catalog"]["negotiated_mode"] == "compact"
        assert info["context_hygiene"]["negotiated_mode"] == "small_context"
        assert info["context_hygiene"]["inference_reason"] == "small_model_profile"

    async def test_initialize_persists_agent_identity_and_claim_uses_session_defaults(self, monkeypatch, tmp_path):
        from app.services import task_lease_service as lease_mod

        monkeypatch.setenv("MNEMOFORGE_AGENT_IDENTITY_PATH", str(tmp_path / ".mnemoforge" / "agent_identity.json"))
        monkeypatch.setenv("MNEMOFORGE_WORKSPACE_ROOT", str(tmp_path))
        store = lease_mod.TaskLeaseStore(Path(":memory:"))
        monkeypatch.setattr(lease_mod, "_STORE", store)
        session_id = "sess-agent-identity-defaults"
        try:
            initialized = await mcp_sse._handle(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {"name": "Codex CLI"},
                        "modelInfo": {"name": "gpt-5"},
                        "_mnemoforge": {"runtime_profile_id": "strong_mcp_operator"},
                    },
                },
                "http://test",
                session_id=session_id,
            )

            identity = initialized["result"]["_mnemoforge"]["agent_identity"]
            assert identity["agent_fingerprint"].startswith("agentfp:")
            assert identity["runtime_profile_id"] == "strong_mcp_operator"

            claimed = await mcp_sse._execute_tool(
                "claim_task",
                {
                    "project": "alpha",
                    "task_id": "task-identity-defaults",
                    "owner_agent": "codex",
                },
                "http://test",
                session_id=session_id,
            )
            claim = json.loads(claimed)
            assert claim["lease"]["agent_fingerprint"] == identity["agent_fingerprint"]
            assert claim["lease"]["runtime_profile_id"] == "strong_mcp_operator"
        finally:
            store.close()

    async def test_claim_task_reclaims_active_session_with_same_fingerprint_and_work_token(self, monkeypatch):
        from app.services import task_lease_service as lease_mod

        store = lease_mod.TaskLeaseStore(Path(":memory:"))
        monkeypatch.setattr(lease_mod, "_STORE", store)
        try:
            first = store.claim(
                project="alpha",
                task_id="task-session-loss",
                owner_agent="codex",
                session_id="sess-lost",
                agent_fingerprint="agentfp:same",
                runtime_profile_id="strong_mcp_operator",
            )

            reclaimed = await mcp_sse._execute_tool(
                "claim_task",
                {
                    "project": "alpha",
                    "task_id": "task-session-loss",
                    "owner_agent": "codex",
                    "session_id": "sess-recovered",
                    "agent_fingerprint": "agentfp:same",
                    "runtime_profile_id": "strong_mcp_operator",
                    "work_token": first.work_token,
                },
                "http://test",
            )

            data = json.loads(reclaimed)
            assert data["status"] == "reclaimed"
            assert data["same_fingerprint_reclaim"] is True
            assert data["lease"]["lease_id"] == first.lease.lease_id
            assert data["lease"]["session_id"] == "sess-recovered"
        finally:
            store.close()

    async def test_mailbox_submit_claim_task_returns_public_lease_receipt(self, monkeypatch):
        from app.services import task_lease_service as lease_mod

        store = lease_mod.TaskLeaseStore(Path(":memory:"))
        monkeypatch.setattr(lease_mod, "_STORE", store)
        try:
            response = await mcp_sse._execute_tool(
                "mailbox_submit",
                {
                    "form_id": "claim_task",
                    "state": "planning",
                    "project": "alpha",
                    "runtime_profile_id": "strong_mcp_operator",
                    "payload": {
                        "project": "alpha",
                        "task_id": "task-mailbox-claim",
                        "owner_agent": "codex",
                        "session_id": "sess-mailbox",
                        "agent_fingerprint": "agentfp:mailbox",
                    },
                },
                "http://test",
            )

            data = json.loads(response)
            assert data["receipt"]["status"] == "claimed"
            assert data["receipt"]["lease"]["task_id"] == "task-mailbox-claim"
            assert data["receipt"]["lease"]["agent_fingerprint"] == "agentfp:mailbox"
            assert "work_token_hash" not in data["receipt"]["lease"]
            assert data["receipt"]["work_token"]
            assert data["next_safe_action"].startswith("Proceed with implementation")
        finally:
            store.close()

    async def test_mailbox_submit_release_task_claim_releases_by_task_id(self, monkeypatch):
        from app.services import task_lease_service as lease_mod

        store = lease_mod.TaskLeaseStore(Path(":memory:"))
        monkeypatch.setattr(lease_mod, "_STORE", store)
        claim = store.claim(
            project="alpha",
            task_id="task-mailbox-release",
            owner_agent="codex",
            session_id="sess-release",
            agent_fingerprint="agentfp:release",
        )
        try:
            response = await mcp_sse._execute_tool(
                "mailbox_submit",
                {
                    "form_id": "release_task_claim",
                    "state": "implementation",
                    "project": "alpha",
                    "payload": {
                        "project": "alpha",
                        "task_id": "task-mailbox-release",
                        "owner_agent": "codex",
                        "session_id": "sess-release",
                        "reason": "test cleanup",
                    },
                },
                "http://test",
            )

            data = json.loads(response)
            assert data["receipt"]["status"] == "released"
            assert data["receipt"]["lease"]["lease_id"] == claim.lease.lease_id
            assert data["receipt"]["lease"]["release_reason"] == "test cleanup"
            assert "work_token_hash" not in data["receipt"]["lease"]
            assert store.get_active_claim(project="alpha", task_id="task-mailbox-release") is None
        finally:
            store.close()

    async def test_mailbox_start_and_finish_task_complete_public_workflow(self, monkeypatch):
        from app.services import stenographer_service as stenographer_mod
        from app.services import task_lease_service as lease_mod

        lease_store = lease_mod.TaskLeaseStore(Path(":memory:"))
        stenographer_store = stenographer_mod.StenographerStore(Path(":memory:"))
        monkeypatch.setattr(lease_mod, "_STORE", lease_store)
        monkeypatch.setattr(stenographer_mod, "_STORE", stenographer_store)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        async def fake_post(api_base: str, path: str, payload: dict):
            if path.startswith("/project/tasks/") and path.endswith("/changes"):
                return {"id": f"checkpoint-{payload.get('stage')}", **payload}
            raise AssertionError(path)

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        try:
            started = await mcp_sse._execute_tool(
                "mailbox_submit",
                {
                    "form_id": "start_task",
                    "state": "planning",
                    "project": "alpha",
                    "runtime_profile_id": "strong_mcp_operator",
                    "payload": {
                        "project": "alpha",
                        "task_id": "task-mailbox-full-cycle",
                        "owner_agent": "codex",
                        "session_id": "sess-mailbox-cycle",
                        "agent_fingerprint": "agentfp:mailbox-cycle",
                        "auto_heartbeat": False,
                    },
                },
                "http://test",
            )
            start_data = json.loads(started)
            assert start_data["receipt"]["status"] == "started"
            assert start_data["receipt"]["lease"]["status"] == "active"
            assert "work_token_hash" not in start_data["receipt"]["lease"]
            assert start_data["receipt"]["work_token"]
            assert start_data["receipt"]["next_state"] == "implementation"
            assert "finish_task" in start_data["receipt"]["next_forms"]
            assert start_data["receipt"]["work_session"]["status"] == "active"

            finished = await mcp_sse._execute_tool(
                "mailbox_submit",
                {
                    "form_id": "finish_task",
                    "state": "checkpointing",
                    "project": "alpha",
                    "runtime_profile_id": "strong_mcp_operator",
                    "payload": {
                        "project": "alpha",
                        "task_id": "task-mailbox-full-cycle",
                        "owner_agent": "codex",
                        "work_token": start_data["receipt"]["work_token"],
                        "summary": "Finished through mailbox.",
                        "changed_files": ["app/routers/mcp_sse.py"],
                        "verification": ["Docker target contour passed."],
                        "next_step": "No follow-up.",
                    },
                },
                "http://test",
            )
            finish_data = json.loads(finished)
            assert finish_data["receipt"]["status"] == "finished"
            assert finish_data["receipt"]["release"]["status"] == "released"
            assert "work_token_hash" not in finish_data["receipt"]["release"]["lease"]
            assert lease_store.get_active_claim(project="alpha", task_id="task-mailbox-full-cycle") is None
        finally:
            lease_store.close()
            stenographer_store.close()

    async def test_mailbox_record_progress_evidence_allows_finish_without_repeating_evidence(self, monkeypatch):
        from app.services import stenographer_service as stenographer_mod
        from app.services import task_lease_service as lease_mod

        lease_store = lease_mod.TaskLeaseStore(Path(":memory:"))
        stenographer_store = stenographer_mod.StenographerStore(Path(":memory:"))
        monkeypatch.setattr(lease_mod, "_STORE", lease_store)
        monkeypatch.setattr(stenographer_mod, "_STORE", stenographer_store)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        async def fake_post(api_base: str, path: str, payload: dict):
            if path.startswith("/project/tasks/") and path.endswith("/changes"):
                return {"id": f"checkpoint-{payload.get('stage')}", **payload}
            raise AssertionError(path)

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        try:
            started = json.loads(
                await mcp_sse._execute_tool(
                    "submit",
                    {
                        "form_id": "start_task",
                        "state": "planning",
                        "project": "alpha",
                        "payload": {
                            "project": "alpha",
                            "task_id": "task-mailbox-progress-finish",
                            "owner_agent": "codex",
                            "agent_fingerprint": "agentfp:progress-finish",
                            "auto_heartbeat": False,
                        },
                    },
                    "http://test",
                )
            )
            assert started["receipt"]["status"] == "started"
            token = started["receipt"]["work_token"]

            progress = json.loads(
                await mcp_sse._execute_tool(
                    "submit",
                    {
                        "form_id": "record_progress",
                        "state": "implementation",
                        "project": "alpha",
                        "payload": {
                            "project": "alpha",
                            "task_id": "task-mailbox-progress-finish",
                            "owner_agent": "codex",
                            "work_token": token,
                            "summary": "Recorded closeout evidence once.",
                            "changed_files": ["app/services/mcp_mailbox_actions.py"],
                            "verification": ["Docker contour passed."],
                            "next_step": "No follow-up.",
                            "stage": "handoff",
                        },
                    },
                    "http://test",
                )
            )
            assert progress["receipt"]["status"] == "accepted"

            finished = json.loads(
                await mcp_sse._execute_tool(
                    "submit",
                    {
                        "form_id": "finish_task",
                        "state": "handoff",
                        "project": "alpha",
                        "payload": {
                            "project": "alpha",
                            "task_id": "task-mailbox-progress-finish",
                            "owner_agent": "codex",
                            "work_token": token,
                            "summary": "Finished without repeating evidence.",
                        },
                    },
                    "http://test",
                )
            )
            assert finished["receipt"]["status"] == "finished"
            assert finished["receipt"]["release"]["status"] == "released"
        finally:
            lease_store.close()
            stenographer_store.close()

    async def test_mailbox_finish_accepts_explicit_empty_changed_files(self, monkeypatch):
        from app.services import stenographer_service as stenographer_mod
        from app.services import task_lease_service as lease_mod

        lease_store = lease_mod.TaskLeaseStore(Path(":memory:"))
        stenographer_store = stenographer_mod.StenographerStore(Path(":memory:"))
        monkeypatch.setattr(lease_mod, "_STORE", lease_store)
        monkeypatch.setattr(stenographer_mod, "_STORE", stenographer_store)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        async def fake_post(api_base: str, path: str, payload: dict):
            if path.startswith("/project/tasks/") and path.endswith("/changes"):
                return {"id": f"checkpoint-{payload.get('stage')}", **payload}
            raise AssertionError(path)

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        try:
            started = json.loads(
                await mcp_sse._execute_tool(
                    "submit",
                    {
                        "form_id": "start_task",
                        "state": "planning",
                        "project": "alpha",
                        "payload": {
                            "project": "alpha",
                            "task_id": "task-mailbox-empty-changes",
                            "owner_agent": "codex",
                            "auto_heartbeat": False,
                        },
                    },
                    "http://test",
                )
            )
            token = started["receipt"]["work_token"]
            work_id = started["receipt"]["work_session"]["work_id"]

            finished = json.loads(
                await mcp_sse._execute_tool(
                    "submit",
                    {
                        "form_id": "finish_task",
                        "state": "handoff",
                        "project": "alpha",
                        "payload": {
                            "project": "alpha",
                            "task_id": "task-mailbox-empty-changes",
                            "owner_agent": "codex",
                            "work_token": token,
                            "work_id": work_id,
                            "summary": "Finished with explicit no file changes.",
                            "changed_files": [],
                            "verification": ["finish_task fix verified"],
                            "next_step": "none",
                        },
                    },
                    "http://test",
                )
            )

            assert finished["receipt"]["status"] == "finished"
            spans = stenographer_store.list_spans(
                project="alpha",
                task_id="task-mailbox-empty-changes",
                work_id=work_id,
            )
            changed = [span for span in spans if span.kind == "changed_files"]
            assert [span.content for span in changed] == ["none"]
        finally:
            lease_store.close()
            stenographer_store.close()

    async def test_mailbox_start_task_converts_internal_error_to_public_receipt(self, monkeypatch):
        async def fake_execute_tool(name: str, args: dict, api_base: str, session_id: str | None = None):
            raise RuntimeError('HTTP 404: {"detail":"Task not found"}')

        async def fake_post(api_base: str, path: str, payload: dict):
            raise AssertionError("start_task should not call post directly")

        async def fake_identity_defaults(session_id: str | None):
            return {}

        form = mcp_sse.mailbox_form_by_id("start_task")
        assert form is not None

        packet = await mailbox_start_task(
            form=form,
            payload={"project": "alpha", "task_id": "missing-task"},
            state="planning",
            project="alpha",
            runtime_profile_id="strong_mcp_operator",
            diagnostic=False,
            api_base="http://test",
            session_id="sess-start",
            dependencies=MailboxActionDependencies(
                post=fake_post,
                execute_tool=fake_execute_tool,
                get_session_identity_defaults=fake_identity_defaults,
                task_mutation_guard=lambda **kwargs: None,
            ),
        )

        assert packet["receipt"]["status"] == "conflict"
        assert packet["receipt"]["form_id"] == "start_task"
        assert packet["receipt"]["message"] == "Requested task or route was not found."
        assert "http" not in packet["receipt"]["message"].casefold()
        assert "get_task_context" in packet["receipt"]["next_safe_action"]

    async def test_mailbox_start_task_conflict_returns_same_fingerprint_recovery_receipt(self, monkeypatch):
        async def fake_execute_tool(name: str, args: dict, api_base: str, session_id: str | None = None):
            assert name == "start_task_session"
            return json.dumps(
                {
                    "status": "conflict",
                    "error": {
                        "error": "task_already_claimed",
                        "active_lease": {
                            "lease_id": "lease-1",
                            "project": "alpha",
                            "task_id": args["task_id"],
                            "owner_agent": "codex",
                            "session_id": "lost-session",
                            "agent_fingerprint": "agentfp:stable",
                            "runtime_profile_id": "codex_cli",
                            "status": "active",
                            "expires_at": "2026-05-24T08:10:00Z",
                            "work_token_hash": "secret-hash",
                        },
                    },
                    "next_safe_action": "Task is already claimed by another session.",
                }
            )

        async def fake_post(api_base: str, path: str, payload: dict):
            raise AssertionError("start_task conflict should not call post")

        async def fake_identity_defaults(session_id: str | None):
            return {}

        form = mcp_sse.mailbox_form_by_id("start_task")
        assert form is not None

        packet = await mailbox_start_task(
            form=form,
            payload={
                "project": "alpha",
                "task_id": "task-1",
                "owner_agent": "codex",
                "agent_fingerprint": "agentfp:stable",
                "work_token": "known-token",
            },
            state="planning",
            project="alpha",
            runtime_profile_id="codex_cli",
            diagnostic=False,
            api_base="http://test",
            session_id="recovered-session",
            dependencies=MailboxActionDependencies(
                post=fake_post,
                execute_tool=fake_execute_tool,
                get_session_identity_defaults=fake_identity_defaults,
                task_mutation_guard=lambda **kwargs: None,
            ),
        )

        receipt = packet["receipt"]
        assert receipt["status"] == "conflict"
        assert receipt["same_fingerprint"] is True
        assert receipt["lease"]["lease_id"] == "lease-1"
        assert "work_token_hash" not in receipt["lease"]
        assert receipt["recommended_reclaim_call"] == {
            "tool": "submit",
            "form_id": "start_task",
            "state": "planning",
            "project": "alpha",
            "payload": {
                "project": "alpha",
                "task_id": "task-1",
                "owner_agent": "codex",
                "agent_fingerprint": "agentfp:stable",
                "work_token": "known-token",
            },
        }
        assert receipt["recovery_options"][1]["id"] == "same_fingerprint_reclaim"

    def test_simple_submit_compact_keeps_start_task_recovery_fields(self):
        from app.services.mcp_simple_surface_actions import compact_simple_submit_packet

        data = {
            "receipt": {
                "status": "conflict",
                "form_id": "start_task",
                "message": "Task is already claimed by this same agent fingerprint.",
                "same_fingerprint": True,
                "recovery_options": [{"id": "same_fingerprint_reclaim"}],
                "recommended_reclaim_call": {"tool": "submit", "form_id": "start_task"},
                "next_safe_action": "Submit recommended_reclaim_call to recover the same active work session.",
            }
        }

        compact = compact_simple_submit_packet(
            data,
            {},
            tool_surface_role=lambda tool_name: "public_entrypoint" if tool_name in {"submit", "get"} else "specialized",
        )

        assert compact["receipt"]["same_fingerprint"] is True
        assert compact["receipt"]["recovery_options"][0]["id"] == "same_fingerprint_reclaim"
        assert compact["receipt"]["recommended_reclaim_call"]["tool"] == "submit"

    async def test_mailbox_finish_task_wraps_finish_session_with_public_receipt(self, monkeypatch):
        form = mcp_sse.mailbox_form_by_id("finish_task")
        assert form is not None

        async def fake_execute_tool(name: str, args: dict, api_base: str, session_id: str | None = None):
            assert name == "finish_task_session"
            assert args["source"] == "mailbox_submit.finish_task"
            return json.dumps(
                {
                    "status": "finished",
                    "task_id": args["task_id"],
                    "release": {
                        "status": "released",
                        "lease": {
                            "lease_id": "lease-1",
                            "task_id": args["task_id"],
                            "status": "released",
                            "work_token_hash": "secret",
                        },
                    },
                    "work_session": {"work_id": "work-1"},
                    "checkpoint": {"id": "checkpoint-1"},
                }
            )

        async def fake_post(api_base: str, path: str, payload: dict):
            raise AssertionError("finish_task should not call post directly")

        async def fake_identity_defaults(session_id: str | None):
            return {}

        packet = await mailbox_finish_task(
            form=form,
            payload={
                "project": "alpha",
                "task_id": "task-mailbox-finish",
                "summary": "Finished via mailbox.",
                "changed_files": ["app/example.py"],
                "verification": ["Docker contour passed."],
                "next_step": "No follow-up.",
            },
            state="checkpointing",
            project="alpha",
            runtime_profile_id="strong_mcp_operator",
            diagnostic=False,
            api_base="http://test",
            session_id="sess-finish",
            dependencies=MailboxActionDependencies(
                post=fake_post,
                execute_tool=fake_execute_tool,
                get_session_identity_defaults=fake_identity_defaults,
                task_mutation_guard=lambda **kwargs: None,
            ),
        )

        assert packet["receipt"]["status"] == "finished"
        assert packet["receipt"]["release"]["status"] == "released"
        assert "work_token_hash" not in packet["receipt"]["release"]["lease"]
        assert "_internal" not in packet

    async def test_initialize_explicit_full_overrides_small_window_inference(self):
        initialized = await mcp_sse._handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "Debug Agent"},
                    "modelInfo": {"contextWindow": 32000},
                    "_mnemoforge": {
                        "tool_catalog": {"preferred_mode": "full"},
                        "context": {"hygiene_mode": "full"},
                    },
                },
            },
            "http://test",
            session_id="sess-small-context-full-override",
        )

        info = initialized["result"]["_mnemoforge"]
        assert info["tool_catalog"]["negotiated_mode"] == "full"
        assert "inferred" not in info["tool_catalog"]
        assert info["context_hygiene"]["negotiated_mode"] == "full"
        assert "inferred" not in info["context_hygiene"]

    async def test_tools_call_small_context_hides_service_keys(self, monkeypatch):
        async def fake_execute_tool(name: str, args: dict, api_base: str, session_id: str | None = None):
            return json.dumps(
                {
                    "project": "alpha",
                    "state": "documentation",
                    "readiness": {"ready_to_enter": True},
                    "required_rules": [
                        {
                            "id": "law-1",
                            "title": "Internal Text Is English",
                            "scope": "principle",
                            "status": "active",
                            "rationale": "Long service-facing rationale.",
                            "reason": "Matched required terms.",
                        }
                    ],
                    "token_budget": {"estimated_tokens": 15000},
                    "token_overhead": {"estimated_tokens": 15000},
                    "coverage": {"active_laws_seen": 42},
                    "feedback_expected": True,
                    "follow_up": "tool_feedback",
                },
                ensure_ascii=False,
            )

        monkeypatch.setattr(mcp_sse, "_execute_tool", fake_execute_tool)
        response = await mcp_sse._handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "get_task_execution_context",
                    "arguments": {"task": "Document architecture.", "state": "documentation", "context_hygiene_mode": "small"},
                },
            },
            "http://test",
        )

        text = response["result"]["content"][0]["text"]
        data = json.loads(text)
        assert "token_budget" not in data
        assert "token_overhead" not in data
        assert "coverage" not in data
        assert "feedback_expected" not in data
        assert data["required_rules"][0] == {
            "id": "law-1",
            "title": "Internal Text Is English",
            "scope": "principle",
            "status": "active",
            "reason": "Matched required terms.",
        }
        assert data["_mnemoforge_refs"]["mode"] == "small_context"
        assert data["_mnemoforge_refs"]["omitted_service_fields"] >= 1
        assert "full_response" in data["_mnemoforge_refs"]

    async def test_tools_call_full_context_keeps_service_keys(self, monkeypatch):
        async def fake_execute_tool(name: str, args: dict, api_base: str, session_id: str | None = None):
            return json.dumps({"project": "alpha", "token_budget": {"estimated_tokens": 15000}, "feedback_expected": True})

        monkeypatch.setattr(mcp_sse, "_execute_tool", fake_execute_tool)
        response = await mcp_sse._handle(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "get_task_execution_context",
                    "arguments": {"task": "Document architecture.", "state": "documentation", "context_hygiene_mode": "full"},
                },
            },
            "http://test",
        )

        text = response["result"]["content"][0]["text"]
        data = json.loads(text)
        assert data["token_budget"]["estimated_tokens"] == 15000
        assert data["feedback_expected"] is True

    def test_sse_handoff_task_exposes_bounded_ownership_fields(self):
        tool = next(tool for tool in mcp_sse.TOOLS if tool["name"] == "handoff_task")
        props = tool["inputSchema"]["properties"]
        assert "owner_agent" in props
        assert "write_scope" in props
        assert props["write_scope"]["type"] == "array"

    def test_list_open_tasks_tool_is_exposed(self):
        tool = next(tool for tool in mcp_sse.TOOLS if tool["name"] == "list_open_tasks")
        props = tool["inputSchema"]["properties"]
        assert "project" in props
        assert props["artifact_type"]["default"] == "all"
        assert props["artifact_type"]["enum"] == ["all", "task", "improvement"]
        assert "limit" in props
        assert "status" not in props

    def test_list_active_tasks_is_not_exposed_in_public_catalog(self):
        assert all(tool["name"] != "list_active_tasks" for tool in mcp_sse.TOOLS)

    def test_task_claim_tools_are_exposed(self):
        names = {tool["name"] for tool in mcp_sse.TOOLS}
        assert {"claim_task", "heartbeat_task_claim", "release_task_claim", "list_task_claims"} <= names
        claim_tool = next(tool for tool in mcp_sse.TOOLS if tool["name"] == "claim_task")
        claim_props = claim_tool["inputSchema"]["properties"]
        assert "project" in claim_tool["inputSchema"]["required"]
        assert "task_id" in claim_tool["inputSchema"]["required"]
        assert claim_props["lease_ttl_seconds"]["default"] == 900
        assert "agent_fingerprint" in claim_props
        assert claim_props["runtime_profile_id"]["default"] == "unknown_cli"
        assert "work_token" in claim_props
        start_tool = next(tool for tool in mcp_sse.TOOLS if tool["name"] == "start_task_session")
        start_props = start_tool["inputSchema"]["properties"]
        assert start_props["auto_heartbeat"]["default"] is True
        assert start_props["heartbeat_seconds"]["minimum"] == 1
        assert "agent_fingerprint" in start_props
        assert start_props["runtime_profile_id"]["default"] == "unknown_cli"
        assert "work_token" in start_props
        list_tool = next(tool for tool in mcp_sse.TOOLS if tool["name"] == "list_task_claims")
        list_props = list_tool["inputSchema"]["properties"]
        assert "agent_fingerprint" in list_props
        assert "runtime_profile_id" in list_props

    def test_reconcile_completed_checkpoints_tool_is_exposed_report_only_by_default(self):
        tool = next(tool for tool in mcp_sse.TOOLS if tool["name"] == "reconcile_completed_checkpoints")
        props = tool["inputSchema"]["properties"]
        assert props["close"]["default"] is False
        assert props["close_policy"]["default"] == "strict"
        assert "project" in props
        review_tool = next(tool for tool in mcp_sse.TOOLS if tool["name"] == "review_completed_checkpoint_scope")
        review_props = review_tool["inputSchema"]["properties"]
        assert "task_id" in review_tool["inputSchema"]["required"]
        assert "next_step_scope" in review_tool["inputSchema"]["required"]
        assert "follow_up_task" in review_props["next_step_scope"]["enum"]
        batch_tool = next(tool for tool in mcp_sse.TOOLS if tool["name"] == "review_completed_checkpoint_scopes")
        assert "decisions" in batch_tool["inputSchema"]["required"]
        assert batch_tool["inputSchema"]["properties"]["decisions"]["maxItems"] == 50

    def test_normalize_mcp_intent_tool_is_exposed(self):
        tool = next(tool for tool in mcp_sse.TOOLS if tool["name"] == "normalize_mcp_intent")
        props = tool["inputSchema"]["properties"]
        assert "intent" in props
        assert "project_id" in props

    def test_reopen_task_tool_is_exposed(self):
        tool = next(tool for tool in mcp_sse.TOOLS if tool["name"] == "reopen_task")
        props = tool["inputSchema"]["properties"]
        assert "task_id" in props
        assert "project" in props
        assert props["status"]["default"] == "active"

    def test_tool_discovery_family_tools_are_exposed(self):
        names = {tool["name"] for tool in mcp_sse.TOOLS}
        assert {"list_tool_families", "tool_family_tools", "tool_explain", "tool_recommend", "tool_feedback", "ask_project", "project_work", "project_rules", "project_context", "project_verify", "project_capture", "pull_task_context", "clerk_draft_report", "draft_task_checkpoint", "record_work_result", "record_task_checkpoint", "report_task_checkpoint", "get_task_execution_context", "get_project_reconstruction_bundle", "operational_tray"} <= names

    def test_ask_project_tool_is_human_facing_expert_facade(self):
        tool = next(tool for tool in mcp_sse.TOOLS if tool["name"] == "ask_project")
        schema = tool["inputSchema"]
        assert schema["required"] == ["question"]
        props = schema["properties"]
        assert "query" in props
        assert props["response_format"]["enum"] == ["auto", "answer", "diagnostic", "json"]
        assert props["client_profile"]["enum"] == ["default", "local", "small_context", "agent"]
        assert props["evaluation_footer"]["enum"] == ["none", "routine_reduction"]
        assert props["project"]["default"] == "mnemoforge"
        assert "Human-facing" in tool["description"]

    def test_project_work_tool_is_thematic_routing_facade(self):
        tool = next(tool for tool in mcp_sse.TOOLS if tool["name"] == "project_work")
        schema = tool["inputSchema"]
        assert schema["required"] == ["intent"]
        props = schema["properties"]
        assert "task_id" in props
        assert "artifact_key" in props
        assert "allow_mutation" in props
        assert props["scorer_backend"]["enum"] == ["lexical", "auto", "llm"]
        assert props["scorer_backend"]["default"] == "auto"
        assert props["allow_mutation"]["default"] is False
        assert props["diagnostic"]["default"] is False
        assert props["answer"]["default"] is False
        assert props["response_format"]["enum"] == ["json", "diagnostic", "answer"]
        assert "verification" in props["state"]["enum"]
        assert "live_validation" in props["state"]["enum"]

    def test_project_reconstruction_bundle_tool_is_read_only_recovery_surface(self):
        tool = next(tool for tool in mcp_sse.TOOLS if tool["name"] == "get_project_reconstruction_bundle")
        schema = tool["inputSchema"]
        assert schema["required"] == ["project_id"]
        props = schema["properties"]
        assert props["detail"]["enum"] == ["compact", "full"]
        assert props["max_items_per_layer"]["maximum"] == 50
        assert "source-loss reconstruction bundle" in tool["description"]

    def test_project_rules_tool_is_thematic_governance_facade(self):
        tool = next(tool for tool in mcp_sse.TOOLS if tool["name"] == "project_rules")
        schema = tool["inputSchema"]
        assert schema["required"] == ["intent"]
        props = schema["properties"]
        assert props["allow_mutation"]["default"] is False
        assert "candidate_id" in props
        assert "law_id" in props
        assert props["response_format"]["enum"] == ["json", "diagnostic", "answer"]
        assert props["diagnostic"]["default"] is False
        assert props["answer"]["default"] is False
        assert props["target_status"]["enum"] == ["trial", "candidate", "proposed", "user_confirmed", "active"]
        assert "rule-governance facade" in tool["description"]

    async def test_project_rules_list_laws_defaults_to_all_statuses(self, monkeypatch):
        captured: dict[str, str] = {}

        async def fake_get(api_base: str, path: str):
            captured["path"] = path
            return {
                "items": [
                    {
                        "id": "law-1",
                        "title": "Proposed law",
                        "status": "proposed",
                        "scope": "project",
                        "project": "alpha",
                        "is_project_local": True,
                    }
                ]
            }

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        result = await mcp_sse._execute_tool(
            "project_rules",
            {
                "project": "alpha",
                "intent": "list project laws",
                "response_format": "json",
                "scorer_backend": "lexical",
            },
            "http://test",
        )

        data = json.loads(result)
        assert "status=all" in captured["path"]
        assert data["selected_route"]["tool"] == "list_project_laws"

    async def test_ask_project_read_lookup_with_mutation_tool_names_stays_read_only(self, monkeypatch):
        route = await mcp_sse._ask_project_select_route(
            {
                "project": "alpha",
                "question": "find improvement about promote_rule_candidate and list_project_laws",
                "response_format": "json",
                "scorer_backend": "lexical",
            }
        )

        assert route["guardrail"] == ""
        assert route["facade"] in {"project_context", "project_work"}

    async def test_ask_project_terse_topic_lookup_stays_read_only_artifact_search(self, monkeypatch):
        async def fail_llm(args):
            raise AssertionError("Terse topic lookup must not require LLM routing")

        monkeypatch.setattr(mcp_sse, "_ask_project_llm_route", fail_llm)
        route = await mcp_sse._ask_project_select_route(
            {
                "project": "mnemoforge",
                "question": "HTTP download",
                "response_format": "json",
            }
        )

        assert route["facade"] == "project_context"
        assert route["structural_match"] is True
        assert route["payload"]["artifact_lookup"] is True
        assert "allow_mutation" not in route["payload"]

        context_route = mcp_sse._project_context_route(route["payload"])
        assert context_route["tool"] == "list_artifacts"
        assert context_route["mutating"] is False
        assert context_route["payload"]["query"] == "HTTP download"

    def test_get_find_task_query_maps_to_task_artifact_list(self):
        assert explicit_artifact_list_type("find task about HTTP download") == "task"

    def test_artifact_lookup_answer_explains_topic_alias_match(self):
        answer = mcp_sse._format_route_answer(
            {
                "facade": "project_context",
                "executed": True,
                "selected_route": {"tool": "list_artifacts", "intent_type": "artifact_lookup", "mutating": False},
                "result": {
                    "items": [
                        {
                            "task_id": "493065dd-6719-431b-a91a-4a080204d5b4",
                            "title": "Scripted portable data export and import-preview workflows",
                            "status": "open",
                            "artifact_key": "task:mnemoforge:493065dd-6719-431b-a91a-4a080204d5b4",
                            "matched_topic_tags": ["#http-download", "#data-portability"],
                            "match_reason": "Matched topic aliases #http-download, #data-portability.",
                        }
                    ]
                },
            }
        )

        assert "matched_topic_tags=#http-download; #data-portability" in answer
        assert "why_match=Matched topic aliases" in answer

    def test_route_payload_template_suppresses_tool_like_artifact_query(self):
        route = mcp_sse._project_context_route({"project": "mnemoforge", "intent": "list_artifacts", "artifact_lookup": True})

        assert route["tool"] == "list_artifacts"
        assert "query" not in route["payload"]

    def test_project_context_tool_is_thematic_context_facade(self):
        tool = next(tool for tool in mcp_sse.TOOLS if tool["name"] == "project_context")
        schema = tool["inputSchema"]
        assert schema["required"] == ["intent"]
        props = schema["properties"]
        assert props["detail"]["enum"] == ["compact", "full"]
        assert props["context_profile"]["default"] == "hot_path"
        assert props["diagnostic"]["default"] is False
        assert props["answer"]["default"] is False
        assert props["response_format"]["enum"] == ["json", "diagnostic", "answer"]
        assert props["scorer_backend"]["enum"] == ["lexical", "auto", "llm"]
        assert props["scorer_backend"]["default"] == "auto"
        assert "project-context facade" in tool["description"]

    def test_project_verify_tool_is_thematic_verification_facade(self):
        tool = next(tool for tool in mcp_sse.TOOLS if tool["name"] == "project_verify")
        schema = tool["inputSchema"]
        assert schema["required"] == ["intent"]
        props = schema["properties"]
        assert "verification" in props["state"]["enum"]
        assert "live_validation" in props["state"]["enum"]
        assert props["diagnostic"]["default"] is False
        assert props["answer"]["default"] is False
        assert props["response_format"]["enum"] == ["json", "diagnostic", "answer"]
        assert props["scorer_backend"]["enum"] == ["lexical", "auto", "llm"]
        assert props["scorer_backend"]["default"] == "auto"
        assert "project-defined verification contour refs" in tool["description"]
        assert "120-second post-restart window" not in tool["description"]

    def test_project_capture_tool_is_thematic_capture_facade(self):
        tool = next(tool for tool in mcp_sse.TOOLS if tool["name"] == "project_capture")
        schema = tool["inputSchema"]
        assert schema["required"] == ["intent"]
        props = schema["properties"]
        assert props["allow_mutation"]["default"] is False
        assert props["diagnostic"]["default"] is False
        assert props["answer"]["default"] is False
        assert props["response_format"]["enum"] == ["json", "diagnostic", "answer"]
        assert props["scorer_backend"]["enum"] == ["lexical", "auto", "llm"]
        assert props["scorer_backend"]["default"] == "auto"
        assert "raw_notes" in props
        assert "stenographer/clerk drafts" in tool["description"]

    def test_mailbox_state_tool_is_public_read_only_entrypoint(self):
        tool = next(tool for tool in mcp_sse.TOOLS if tool["name"] == "mailbox_state")
        schema = tool["inputSchema"]
        assert schema["required"] == ["state"]
        props = schema["properties"]
        assert "planning" in props["state"]["enum"]
        assert "verification" in props["state"]["enum"]
        assert props["runtime_profile_id"]["default"] == "unknown_cli"
        assert props["diagnostic"]["default"] is False
        assert "public state packet" in tool["description"]

    def test_mailbox_submit_tool_is_public_guarded_entrypoint(self):
        tool = next(tool for tool in mcp_sse.TOOLS if tool["name"] == "mailbox_submit")
        schema = tool["inputSchema"]
        assert schema["required"] == ["form_id", "payload"]
        props = schema["properties"]
        assert "planning" in props["state"]["enum"]
        assert "verification" in props["state"]["enum"]
        assert props["runtime_profile_id"]["default"] == "unknown_cli"
        assert props["diagnostic"]["default"] is False
        assert "public receipt" in tool["description"]

    def test_mailbox_get_tool_is_public_read_entrypoint(self):
        tool = next(tool for tool in mcp_sse.TOOLS if tool["name"] == "mailbox_get")
        schema = tool["inputSchema"]
        assert schema["required"] == ["ref"]
        props = schema["properties"]
        assert "planning" in props["state"]["enum"]
        assert "verification" in props["state"]["enum"]
        assert props["runtime_profile_id"]["default"] == "unknown_cli"
        assert props["diagnostic"]["default"] is False
        assert "public mailbox packet" in tool["description"]

    def test_report_issue_tool_accepts_human_importance_scale_hint(self):
        tool = next(tool for tool in mcp_sse.TOOLS if tool["name"] == "report_issue")
        importance = tool["inputSchema"]["properties"]["importance_score"]
        assert importance["maximum"] == 10
        assert "1..10 shorthand" in importance["description"]

    def test_record_work_result_tool_is_high_level_closeout_facade(self):
        tool = next(tool for tool in mcp_sse.TOOLS if tool["name"] == "record_work_result")
        schema = tool["inputSchema"]
        assert schema["required"] == ["summary"]
        props = schema["properties"]
        assert "task_id" in props
        assert "artifact_key" in props
        assert props["should_resolve_artifact"]["default"] is False
        assert props["create_issue_if_unmatched"]["default"] is False
        assert props["use_clerk"]["default"] is False

    def test_clerk_draft_report_tool_is_first_class_closeout_surface(self):
        tool = next(tool for tool in mcp_sse.TOOLS if tool["name"] == "clerk_draft_report")
        props = tool["inputSchema"]["properties"]
        assert "work_id" in props
        assert "raw_notes" in props
        assert props["preserve_evidence"]["default"] is True
        assert props["use_llm"]["default"] is False

    def test_operational_tray_tool_is_state_aware_facade(self):
        tool = next(tool for tool in mcp_sse.TOOLS if tool["name"] == "operational_tray")
        schema = tool["inputSchema"]
        assert {"task", "state", "action"} <= set(schema["required"])
        assert schema["properties"]["action"]["enum"] == ["inspect", "execute"]
        assert "record_stage_evidence" in schema["properties"]["tray_action"]["enum"]
        assert "args" in schema["properties"]
        assert "tool" in schema["properties"]
        assert "arguments" in schema["properties"]
        assert "work_token" in schema["properties"]

    def test_upsert_knowledge_tree_node_tool_is_exposed(self):
        tool = next(tool for tool in mcp_sse.TOOLS if tool["name"] == "upsert_knowledge_tree_node")
        schema = tool["inputSchema"]
        assert {"topic_path", "title"} <= set(schema["required"])
        assert "responsibility" in schema["properties"]
        assert "projection_targets" in schema["properties"]

    def test_task_execution_context_tool_is_state_scoped(self):
        tool = next(tool for tool in mcp_sse.TOOLS if tool["name"] == "get_task_execution_context")
        schema = tool["inputSchema"]
        assert {"task", "state"} <= set(schema["required"])
        assert "verification" in schema["properties"]["state"]["enum"]
        assert "live_validation" in schema["properties"]["state"]["enum"]
        assert "documentation" in schema["properties"]["state"]["enum"]
        assert "stage_evidence" in schema["properties"]
        assert "prior_stage_recorded" in schema["properties"]

    async def test_task_execution_context_tool_posts_state_payload(self, monkeypatch):
        posted: list[tuple[str, dict]] = []

        async def fake_post(api_base: str, path: str, payload: dict):
            posted.append((path, payload))
            return {
                "project": payload["project"],
                "state": payload["state"],
                "task": payload["task"],
                "required_rules": [],
                "recommended_rules": [],
                "recommended_tools": [],
                "risk_controls": [],
                "expected_outputs": [],
                "next_transitions": [],
                "rationale": "test",
                "coverage": {},
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        result = await mcp_sse._execute_tool(
            "get_task_execution_context",
            {
                "project": "alpha",
                "task": "Verify server change",
                "state": "verification",
                "changed_files": ["app/main.py"],
                "stage_evidence": ["checkpoint:implementation-1"],
                "prior_stage_recorded": True,
            },
            "http://test",
        )

        assert posted[0][0] == "/task-execution-context"
        assert posted[0][1]["state"] == "verification"
        assert posted[0][1]["changed_files"] == ["app/main.py"]
        assert posted[0][1]["stage_evidence"] == ["checkpoint:implementation-1"]
        assert posted[0][1]["prior_stage_recorded"] is True
        assert '"stage": "testing"' in result or '"stage": "stable"' in result

    async def test_mailbox_state_returns_public_packet_without_internal_metadata(self):
        result = await mcp_sse._execute_tool(
            "mailbox_state",
            {
                "project": "alpha",
                "state": "planning",
                "runtime_profile_id": "weak_mcp_operator",
                "diagnostic": True,
            },
            "http://test",
        )

        data = json.loads(result)
        assert data["state"] == "planning"
        assert data["project"] == "alpha"
        assert "_internal" not in data
        assert any(form["form_id"] == "create_improvement" for form in data["forms"])
        assert all("postconditions" not in form for form in data["forms"])
        assert "Internal diagnostics are not available" in data["warnings"][-1]

    async def test_mailbox_state_diagnostic_profile_can_return_internal_metadata(self):
        result = await mcp_sse._execute_tool(
            "mailbox_state",
            {
                "project": "alpha",
                "state": "verification",
                "runtime_profile_id": "diagnostic_operator",
                "diagnostic": True,
            },
            "http://test",
        )

        data = json.loads(result)
        assert data["state"] == "verification"
        assert "_internal" in data
        assert data["_internal"]["visibility"] == "internal"
        assert any(form["form_id"] == "run_verification" for form in data["forms"])
        assert any(form["form_id"] == "run_verification" for form in data["_internal"]["forms"])

    async def test_mailbox_submit_run_verification_returns_public_receipt(self):
        result = await mcp_sse._execute_tool(
            "mailbox_submit",
            {
                "project": "alpha",
                "state": "verification",
                "form_id": "run_verification",
                "payload": {"project": "alpha", "changed_files": ["app/services/mcp_mailbox.py"]},
                "runtime_profile_id": "weak_mcp_operator",
                "diagnostic": True,
            },
            "http://test",
        )

        data = json.loads(result)
        assert data["state"] == "verification"
        assert data["project"] == "alpha"
        assert "_internal" not in data
        assert data["receipt"]["status"] == "ready"
        assert data["receipt"]["data_ref"] == "verification_contour:alpha:verification"
        assert "approved_command" not in data["receipt"]
        assert data["receipt"]["forbidden_patterns"] == ["host execution_context"]

    async def test_simple_submit_run_verification_keeps_contour_ref_in_compact_receipt(self):
        result = await mcp_sse._execute_tool(
            "submit",
            {
                "project": "alpha",
                "state": "verification",
                "form_id": "run_verification",
                "payload": {"project": "alpha", "changed_files": ["app/services/mcp_mailbox.py"]},
                "runtime_profile_id": "weak_mcp_operator",
            },
            "http://test",
        )

        data = json.loads(result)
        assert data["receipt"]["status"] == "ready"
        assert data["receipt"]["data_ref"] == "verification_contour:alpha:verification"
        assert data["receipt"]["forbidden_patterns"] == ["host execution_context"]
        assert "approved_command" not in data["receipt"]

    async def test_mailbox_submit_create_improvement_executes_governed_write(self, monkeypatch):
        posted: list[tuple[str, dict]] = []

        async def fake_post(api_base: str, path: str, payload: dict):
            posted.append((path, payload))
            return {"id": "task-memory-id", **payload}

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        result = await mcp_sse._execute_tool(
            "mailbox_submit",
            {
                "project": "alpha",
                "state": "planning",
                "form_id": "create_improvement",
                "payload": {
                    "project": "alpha",
                    "title": "Mailbox FSM",
                    "summary": "Keep external forms stable while internals migrate.",
                    "next_step": "Implement governed mutation later.",
                },
            },
            "http://test",
        )

        data = json.loads(result)
        assert data["receipt"]["status"] == "accepted"
        assert data["receipt"]["mode"] == "write"
        assert data["receipt"]["artifact_key"].startswith("improvement:alpha:")
        assert data["receipt"]["id"]
        assert data["receipt"]["task_id"] == data["receipt"]["id"]
        assert data["receipt"]["linked_artifact_key"].startswith("task:alpha:")
        assert "_internal" not in data
        assert posted[0][0] == "/project/tasks"
        assert posted[1][0].startswith("/project/tasks/")

    async def test_simple_submit_create_improvement_allows_explicit_cross_project_target(self, monkeypatch):
        posted: list[tuple[str, dict]] = []

        async def fake_identity(session_id: str | None):
            return {"project_id": "frontend", "runtime_profile_id": "weak_mcp_operator"}

        async def fake_post(api_base: str, path: str, payload: dict):
            posted.append((path, payload))
            return {"id": "task-memory-id", **payload}

        monkeypatch.setattr(mcp_sse, "_get_session_identity_defaults", fake_identity)
        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        result = await mcp_sse._execute_tool(
            "submit",
            {
                "project": "mnemoforge",
                "state": "planning",
                "form_id": "create_improvement",
                "payload": {
                    "project": "mnemoforge",
                    "source_project": "frontend",
                    "title": "Frontend found backend MCP issue",
                    "summary": "A frontend agent found a backend-facing MCP usability gap.",
                    "next_step": "Review in the backend project backlog.",
                },
            },
            "http://test",
            session_id="session-frontend",
        )

        data = json.loads(result)
        assert data["project"] == "mnemoforge"
        assert data["receipt"]["artifact_key"].startswith("improvement:mnemoforge:")
        assert posted[0][0] == "/project/tasks"
        assert posted[0][1]["project"] == "mnemoforge"
        assert "source_project:frontend" in posted[0][1]["tags"]
        assert posted[1][0].startswith("/project/tasks/")
        assert "source_project:frontend" in posted[1][1]["tags"]

    async def test_mailbox_submit_create_improvement_diagnostic_includes_health_metadata(self, monkeypatch):
        async def fake_post(api_base: str, path: str, payload: dict):
            return {"id": "task-memory-id", **payload}

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        result = await mcp_sse._execute_tool(
            "mailbox_submit",
            {
                "project": "alpha",
                "state": "planning",
                "form_id": "create_improvement",
                "payload": {
                    "project": "alpha",
                    "title": "Mailbox FSM diagnostic",
                    "summary": "Expose actual metadata only to diagnostic profiles.",
                    "next_step": "Review postcondition health.",
                },
                "runtime_profile_id": "diagnostic_operator",
                "diagnostic": True,
            },
            "http://test",
        )

        data = json.loads(result)
        assert data["receipt"]["status"] == "accepted"
        assert data["_internal"]["postcondition_health"]["ok"] is True
        assert data["_internal"]["actual_metadata"]["artifact_type"] == "improvement"

    async def test_mailbox_submit_store_memory_executes_public_memory_write(self, monkeypatch):
        posted: list[tuple[str, dict]] = []

        async def fake_post(api_base: str, path: str, payload: dict):
            posted.append((path, payload))
            return {"id": "memory-public-1", **payload}

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        result = await mcp_sse._execute_tool(
            "mailbox_submit",
            {
                "project": "alpha",
                "state": "planning",
                "form_id": "store_memory",
                "payload": {
                    "project": "alpha",
                    "content": "Weak models should prefer the public mailbox interface.",
                    "tags": ["usability"],
                },
            },
            "http://test",
        )

        data = json.loads(result)
        assert posted[0][0] == "/memories"
        assert posted[0][1]["project"] == "alpha"
        assert posted[0][1]["category"] == "mnemoforge:fact"
        assert "stored_fact" in posted[0][1]["tags"]
        assert data["receipt"]["status"] == "accepted"
        assert data["receipt"]["artifact_key"] == "memory:alpha:memory-public-1"

    async def test_mailbox_submit_create_and_confirm_law_use_public_forms(self, monkeypatch):
        posted: list[tuple[str, dict]] = []

        async def fake_post(api_base: str, path: str, payload: dict):
            posted.append((path, payload))
            if path == "/laws":
                return {"id": "law-public-1", "status": payload["status"], **payload}
            if path == "/laws/law-public-1/confirm":
                return {
                    "id": "law-public-1",
                    "project": "alpha",
                    "status": "active" if payload["activate"] else "user_confirmed",
                    "confirmed_by": payload["confirmed_by"],
                }
            raise AssertionError(path)

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        created = await mcp_sse._execute_tool(
            "mailbox_submit",
            {
                "project": "alpha",
                "state": "planning",
                "form_id": "create_law",
                "payload": {
                    "project": "alpha",
                    "title": "Use mailbox forms",
                    "statement": "Agents should prefer public mailbox forms for simple workflows.",
                },
            },
            "http://test",
        )
        create_data = json.loads(created)
        assert posted[0][0] == "/laws"
        assert posted[0][1]["status"] == "proposed"
        assert create_data["receipt"]["artifact_key"] == "law:alpha:law-public-1"

        confirmed = await mcp_sse._execute_tool(
            "mailbox_submit",
            {
                "project": "alpha",
                "state": "operator_review",
                "form_id": "confirm_law",
                "payload": {
                    "project": "alpha",
                    "law_id": "law-public-1",
                    "confirmed_by": "user",
                    "reason": "Approved during mailbox usability review.",
                },
            },
            "http://test",
        )
        confirm_data = json.loads(confirmed)
        assert posted[1][0] == "/laws/law-public-1/confirm"
        assert posted[1][1]["confirmed_by"] == "user"
        assert confirm_data["receipt"]["status"] == "accepted"
        assert confirm_data["receipt"]["artifact_key"] == "law:alpha:law-public-1"

    async def test_mailbox_submit_create_law_requires_confirmation_for_active_status(self):
        result = await mcp_sse._execute_tool(
            "mailbox_submit",
            {
                "project": "alpha",
                "state": "planning",
                "form_id": "create_law",
                "payload": {
                    "project": "alpha",
                    "title": "Unsafe activation",
                    "statement": "This law should not become active silently.",
                    "status": "active",
                },
            },
            "http://test",
        )

        data = json.loads(result)
        assert data["receipt"]["status"] == "needs_input"
        assert data["receipt"]["missing_fields"] == ["confirmed_by"]

    async def test_mailbox_submit_close_task_archives_without_completion(self, monkeypatch):
        calls: list[tuple[str, str, dict | None]] = []

        async def fake_get(api_base: str, path: str):
            calls.append(("GET", path, None))
            return {
                "id": "memory-task-1",
                "task_id": "task-obsolete",
                "project": "alpha",
                "title": "Old task",
                "description": "No longer needed.",
                "agent_id": "codex",
                "status": "planning",
                "source": "test",
                "tags": ["existing"],
            }

        async def fake_post(api_base: str, path: str, payload: dict):
            calls.append(("POST", path, payload))
            if path == "/project/tasks":
                return {"id": "memory-task-1", **payload}
            if path == "/project/tasks/task-obsolete/changes":
                return {"id": "change-1", **payload}
            raise AssertionError(path)

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        result = await mcp_sse._execute_tool(
            "submit",
            {
                "project": "alpha",
                "state": "planning",
                "form_id": "close_task",
                "payload": {
                    "project": "alpha",
                    "task_id": "task-obsolete",
                    "reason": "Superseded by the new mailbox workflow.",
                    "close_status": "superseded",
                    "superseded_by": "task:alpha:new-task",
                    "release_claim": False,
                },
            },
            "http://test",
        )

        data = json.loads(result)
        assert data["receipt"]["status"] == "accepted"
        assert data["receipt"]["artifact_key"] == "task:alpha:task-obsolete"
        assert data["receipt"]["close_status"] == "superseded"
        assert data["receipt"]["task_status"] == "archived"
        assert data["receipt"]["superseded_by"] == "task:alpha:new-task"
        assert calls[0] == ("GET", "/project/tasks/task-obsolete?project=alpha", None)
        assert calls[1][0:2] == ("POST", "/project/tasks")
        assert calls[1][2]["status"] == "archived"
        assert "close_status:superseded" in calls[1][2]["tags"]
        assert calls[2][0:2] == ("POST", "/project/tasks/task-obsolete/changes")
        assert calls[2][2]["change_type"] == "status_change"

    async def test_mailbox_submit_close_task_resolves_linked_improvement_by_default(self, monkeypatch):
        calls: list[tuple[str, str, dict | None]] = []
        linked_improvement_id = "aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa"

        async def fake_get(api_base: str, path: str):
            calls.append(("GET", path, None))
            return {
                "id": "memory-task-1",
                "task_id": "task-obsolete",
                "project": "alpha",
                "title": "Old task",
                "description": "No longer needed.",
                "agent_id": "codex",
                "status": "planning",
                "source": "test",
                "tags": ["existing"],
                "linked_improvement_id": linked_improvement_id,
            }

        async def fake_post(api_base: str, path: str, payload: dict):
            calls.append(("POST", path, payload))
            if path == "/project/tasks":
                return {"id": "memory-task-1", **payload}
            if path == "/project/tasks/task-obsolete/changes":
                return {"id": "change-1", **payload}
            raise AssertionError(path)

        async def fake_patch(api_base: str, path: str, payload: dict | None = None):
            calls.append(("PATCH", path, payload))
            assert path == f"/improvements/{linked_improvement_id}/resolve"
            assert payload is not None
            assert payload["action_source"] == "mailbox_submit.close_task"
            assert "task-obsolete" in payload["reason"]
            return {"id": linked_improvement_id, "status": "resolved"}

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_patch", fake_patch)

        result = await mcp_sse._execute_tool(
            "submit",
            {
                "project": "alpha",
                "state": "planning",
                "form_id": "close_task",
                "payload": {
                    "project": "alpha",
                    "task_id": "task-obsolete",
                    "reason": "Superseded by the new mailbox workflow.",
                    "close_status": "superseded",
                    "release_claim": False,
                },
            },
            "http://test",
        )

        data = json.loads(result)
        assert data["receipt"]["status"] == "accepted"
        assert data["receipt"]["task_status"] == "archived"
        assert any(call[0] == "PATCH" for call in calls), calls
        assert data["receipt"]["linked_improvement_sync"] == {
            "status": "resolved",
            "linked_artifact_key": f"improvement:alpha:{linked_improvement_id}",
        }
        assert calls[2][0] == "PATCH"
        assert calls[3][0:2] == ("POST", "/project/tasks/task-obsolete/changes")

    async def test_mailbox_submit_close_task_resolves_short_task_id(self, monkeypatch):
        from app.services.public_ref_index import get_public_ref_index_store

        get_public_ref_index_store().clear()
        calls: list[tuple[str, str, dict | None]] = []
        full_id = "bf3ee20c-d404-4e49-91c5-6e547f1077dc"

        async def fake_get(api_base: str, path: str):
            calls.append(("GET", path, None))
            if path == "/artifacts?project=alpha&type=task&limit=100":
                return {
                    "items": [
                        {
                            "artifact_key": f"task:alpha:{full_id}",
                            "linked_artifact_key": f"improvement:alpha:{full_id}",
                            "type": "task",
                            "task_id": full_id,
                            "title": "Resolved report task",
                            "status": "open",
                        }
                    ]
                }
            if path == f"/project/tasks/{full_id}?project=alpha":
                return {
                    "id": "memory-task-1",
                    "task_id": full_id,
                    "project": "alpha",
                    "title": "Resolved report task",
                    "description": "No longer needed.",
                    "agent_id": "codex",
                    "status": "planning",
                    "source": "test",
                    "tags": ["existing"],
                }
            raise AssertionError(path)

        async def fake_post(api_base: str, path: str, payload: dict):
            calls.append(("POST", path, payload))
            if path == "/project/tasks":
                return {"id": "memory-task-1", **payload}
            if path == f"/project/tasks/{full_id}/changes":
                return {"id": "change-1", **payload}
            raise AssertionError(path)

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        result = await mcp_sse._execute_tool(
            "submit",
            {
                "project": "alpha",
                "state": "planning",
                "form_id": "close_task",
                "payload": {
                    "project": "alpha",
                    "task_id": "bf3ee20c",
                    "reason": "Resolved issue report.",
                    "close_status": "obsolete",
                    "release_claim": False,
                },
            },
            "http://test",
        )

        data = json.loads(result)
        assert data["receipt"]["status"] == "accepted"
        assert data["receipt"]["artifact_key"] == f"task:alpha:{full_id}"
        assert calls[0] == ("GET", "/artifacts?project=alpha&type=task&limit=100", None)
        assert calls[1] == ("GET", f"/project/tasks/{full_id}?project=alpha", None)
        assert calls[2][2]["task_id"] == full_id

    async def test_mailbox_submit_create_improvement_bootstraps_task_id(self, monkeypatch):
        from uuid import UUID

        from app.services import improvements_store as improvements_store_mod

        improvement_id = UUID("aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa")
        calls: list[tuple[str, dict]] = []

        class FakeImprovementStore:
            async def upsert_by_title(self, **kwargs):
                return improvement_id, True

            async def get(self, uid):
                assert uid == improvement_id
                return {
                    "id": str(improvement_id),
                    "project": "alpha",
                    "title": "Mailbox-created improvement",
                    "description": "Create a linked task immediately.",
                    "status": "open",
                }

        async def fake_post(api_base: str, path: str, payload: dict):
            calls.append((path, payload))
            if path == "/project/tasks":
                return {"id": "task-memory-id", **payload}
            if path == f"/project/tasks/{improvement_id}/changes":
                return {"id": "change-1", **payload}
            raise AssertionError(path)

        monkeypatch.setattr(improvements_store_mod, "get_improvements_store", lambda: FakeImprovementStore())
        monkeypatch.setattr(mcp_sse, "_post", fake_post)

        result = await mcp_sse._execute_tool(
            "submit",
            {
                "project": "alpha",
                "state": "planning",
                "form_id": "create_improvement",
                "payload": {
                    "project": "alpha",
                    "title": "Mailbox-created improvement",
                    "summary": "Create a linked task immediately.",
                    "next_step": "Use the returned task_id for follow-up forms.",
                },
            },
            "http://test",
        )

        data = json.loads(result)
        assert data["receipt"]["status"] == "accepted"
        assert data["receipt"]["artifact_key"] == f"improvement:alpha:{improvement_id}"
        assert data["receipt"]["task_id"] == str(improvement_id)
        assert data["receipt"]["linked_artifact_key"] == f"task:alpha:{improvement_id}"
        assert data["receipt"]["task_status"] == "planning"
        assert calls[0][0] == "/project/tasks"
        assert calls[0][1]["task_id"] == str(improvement_id)
        assert calls[0][1]["linked_improvement_id"] == str(improvement_id)
        assert calls[1][0] == f"/project/tasks/{improvement_id}/changes"

    async def test_mailbox_submit_set_feature_gate_updates_runtime_gate(self):
        result = await mcp_sse._execute_tool(
            "mailbox_submit",
            {
                "project": "alpha",
                "state": "planning",
                "form_id": "set_feature_gate",
                "payload": {
                    "feature_id": "project_capture_facade",
                    "enabled": False,
                    "scope": "project",
                    "reason": "quarantine misroute during mailbox migration",
                },
            },
            "http://test",
        )

        data = json.loads(result)
        assert data["receipt"]["status"] == "accepted"
        assert data["receipt"]["feature_id"] == "project_capture_facade"
        assert data["receipt"]["scope"] == "project"
        assert data["receipt"]["scope_id"] == "alpha"
        assert data["receipt"]["enabled"] is False

    async def test_mailbox_submit_route_feedback_disables_learned_pattern(self, monkeypatch):
        from app.services import mcp_mailbox_actions

        calls: list[tuple[str, dict]] = []

        class FakeRoutePatternStore:
            def match(self, *, facade: str, pattern: str, allowed_intent_types=None, semantic_threshold: float = 0.6):
                calls.append(("match", {"facade": facade, "pattern": pattern}))
                return {
                    "pattern_id": "learned-route-1",
                    "tool": "project_work",
                    "intent_type": "next_priority",
                }

            def record_feedback(self, pattern_id: str, *, vote: str, reason: str = "", metadata: dict | None = None):
                calls.append(("feedback", {"pattern_id": pattern_id, "vote": vote, "reason": reason, "metadata": metadata or {}}))
                return {"pattern_id": pattern_id, "positive_feedback": 0, "negative_feedback": 1}

            def disable_pattern(self, pattern_id: str, *, reason: str, metadata: dict | None = None):
                calls.append(("disable", {"pattern_id": pattern_id, "reason": reason, "metadata": metadata or {}}))
                return True

        monkeypatch.setattr(mcp_mailbox_actions, "get_route_pattern_store", lambda: FakeRoutePatternStore())

        result = await mcp_sse._execute_tool(
            "submit",
            {
                "project": "alpha",
                "state": "operator_review",
                "form_id": "route_feedback",
                "payload": {
                    "project": "alpha",
                    "facade": "ask_project",
                    "query": "find memory with content usability report",
                    "expected_tool": "memory_search",
                    "actual_tool": "project_work",
                    "reason": "Observed read query routed to work facade.",
                },
            },
            "http://test",
        )

        data = json.loads(result)
        assert data["receipt"]["status"] == "accepted"
        assert data["receipt"]["pattern_id"] == "learned-route-1"
        assert data["receipt"]["feedback_action"] == "disabled"
        assert calls[0][0] == "match"
        assert calls[1][0] == "feedback"
        assert calls[2][0] == "disable"
        assert calls[2][1]["metadata"]["expected_tool"] == "memory_search"

    async def test_mailbox_submit_route_feedback_can_reinforce_learned_pattern(self, monkeypatch):
        from app.services import mcp_mailbox_actions

        calls: list[tuple[str, dict]] = []

        class FakeRoutePatternStore:
            def record_feedback(self, pattern_id: str, *, vote: str, reason: str = "", metadata: dict | None = None):
                calls.append(("feedback", {"pattern_id": pattern_id, "vote": vote, "reason": reason, "metadata": metadata or {}}))
                return {"pattern_id": pattern_id, "positive_feedback": 1, "negative_feedback": 0}

            def disable_pattern(self, pattern_id: str, *, reason: str, metadata: dict | None = None):
                raise AssertionError("positive feedback must not disable useful learned phrases")

        monkeypatch.setattr(mcp_mailbox_actions, "get_route_pattern_store", lambda: FakeRoutePatternStore())

        result = await mcp_sse._execute_tool(
            "submit",
            {
                "project": "alpha",
                "state": "operator_review",
                "form_id": "route_feedback",
                "payload": {
                    "project": "alpha",
                    "facade": "project_work",
                    "pattern_id": "useful-route-1",
                    "vote": "positive",
                    "language": "ru",
                    "phrase_family": "list active work",
                    "jargon_terms": ["podnimi"],
                    "reason": "User-native phrase correctly lists active tasks.",
                },
            },
            "http://test",
        )

        data = json.loads(result)
        assert data["receipt"]["status"] == "accepted"
        assert data["receipt"]["feedback_action"] == "positive_recorded"
        assert data["receipt"]["vote"] == "positive"
        assert calls[0][1]["metadata"]["language"] == "ru"
        assert calls[0][1]["metadata"]["jargon_terms"] == ["podnimi"]

    async def test_mailbox_submit_route_feedback_can_create_positive_alias_for_typo(self, monkeypatch):
        from app.services import mcp_mailbox_actions

        calls: list[tuple[str, dict]] = []

        class FakeRoutePatternStore:
            def match(self, *, facade: str, pattern: str, allowed_intent_types=None, semantic_threshold: float = 0.6):
                calls.append(("match", {"facade": facade, "pattern": pattern}))
                return None

            def record(self, **kwargs):
                calls.append(("record", kwargs))
                return "alias-route-1"

            def record_feedback(self, pattern_id: str, *, vote: str, reason: str = "", metadata: dict | None = None):
                calls.append(("feedback", {"pattern_id": pattern_id, "vote": vote, "reason": reason, "metadata": metadata or {}}))
                return {"pattern_id": pattern_id, "positive_feedback": 1, "negative_feedback": 0}

        monkeypatch.setattr(mcp_mailbox_actions, "get_route_pattern_store", lambda: FakeRoutePatternStore())

        result = await mcp_sse._execute_tool(
            "submit",
            {
                "project": "alpha",
                "state": "operator_review",
                "form_id": "route_feedback",
                "payload": {
                    "project": "alpha",
                    "facade": "project_work",
                    "query": "gjrf;b frnbdyst pflfxb",
                    "vote": "positive",
                    "expected_tool": "list_open_tasks",
                    "expected_intent_type": "next_priority",
                    "keyboard_layout_terms": ["gjrf;b"],
                    "typo_terms": ["pflfxb"],
                    "reason": "Wrong keyboard layout phrase should list active tasks.",
                },
            },
            "http://test",
        )

        data = json.loads(result)
        assert data["receipt"]["status"] == "accepted"
        assert data["receipt"]["pattern_id"] == "alias-route-1"
        assert calls[1][0] == "record"
        assert calls[1][1]["tool"] == "list_open_tasks"
        assert calls[2][1]["metadata"]["keyboard_layout_terms"] == ["gjrf;b"]
        assert calls[2][1]["metadata"]["typo_terms"] == ["pflfxb"]

    async def test_mailbox_submit_route_hygiene_returns_operator_report(self, monkeypatch):
        from app.services import mcp_mailbox_actions

        class FakeRoutePatternStore:
            def hygiene_report(self, *, known_tools: set[str] | None = None, limit: int = 100, stale_after_days: int = 30):
                assert "list_open_tasks" in (known_tools or set())
                return {
                    "status": "ok",
                    "summary": {"active_patterns": 1, "disabled_patterns": 0, "findings": 1},
                    "findings": [{"type": "negative_feedback", "facade": "project_work", "pattern_id": "route-1"}],
                    "patterns": [{"pattern_id": "route-1", "facade": "project_work", "tool": "list_open_tasks"}],
                    "disabled_patterns": [],
                }

        monkeypatch.setattr(mcp_mailbox_actions, "get_route_pattern_store", lambda: FakeRoutePatternStore())

        result = await mcp_sse._execute_tool(
            "submit",
            {
                "project": "alpha",
                "state": "operator_review",
                "form_id": "route_hygiene",
                "payload": {"project": "alpha", "facade": "project_work", "limit": 20},
            },
            "http://test",
        )

        data = json.loads(result)
        assert data["receipt"]["status"] == "accepted"
        assert data["result"]["summary"]["facade_filter"] == "project_work"
        assert data["result"]["summary"]["returned_findings"] == 1

    async def test_mailbox_submit_record_progress_without_task_records_memory(self, monkeypatch):
        posted: list[tuple[str, dict]] = []

        async def fake_post(api_base: str, path: str, payload: dict):
            posted.append((path, payload))
            return {"id": "memory-1", **payload}

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        result = await mcp_sse._execute_tool(
            "mailbox_submit",
            {
                "project": "alpha",
                "state": "implementation",
                "form_id": "record_progress",
                "payload": {
                    "project": "alpha",
                    "summary": "Implemented direct mailbox progress route.",
                    "stage": "in_progress",
                },
            },
            "http://test",
        )

        data = json.loads(result)
        assert posted[0][0] == "/memories"
        assert posted[0][1]["category"] == "mnemoforge:progress"
        assert data["receipt"]["status"] == "accepted"
        assert data["receipt"]["id"] == "memory-1"
        assert data["receipt"]["stage"] == "in_progress"

    async def test_mailbox_submit_record_progress_with_task_requires_owned_claim(self):
        result = await mcp_sse._execute_tool(
            "mailbox_submit",
            {
                "project": "alpha",
                "state": "implementation",
                "form_id": "record_progress",
                "payload": {
                    "project": "alpha",
                    "task_id": "task-1",
                    "summary": "Task progress needs lease proof.",
                },
            },
            "http://test",
        )

        data = json.loads(result)
        assert data["receipt"]["status"] == "conflict"
        assert "active owned claim" in data["receipt"]["message"]
        assert "same task_id and agent_fingerprint" in data["receipt"]["next_safe_action"]
        assert data["receipt"]["recommended_reclaim_call"] == {
            "tool": "submit",
            "form_id": "start_task",
            "state": "planning",
            "project": "alpha",
            "payload": {
                "project": "alpha",
                "task_id": "task-1",
            },
        }

    async def test_simple_submit_start_task_compact_keeps_work_token_and_next_forms(self, monkeypatch):
        from app.services import stenographer_service as stenographer_mod
        from app.services import task_lease_service as lease_mod

        lease_store = lease_mod.TaskLeaseStore(Path(":memory:"))
        stenographer_store = stenographer_mod.StenographerStore(Path(":memory:"))
        monkeypatch.setattr(lease_mod, "_STORE", lease_store)
        monkeypatch.setattr(stenographer_mod, "_STORE", stenographer_store)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        async def fake_post(api_base: str, path: str, payload: dict):
            return {"id": "checkpoint-start", **payload}

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        try:
            result = await mcp_sse._execute_tool(
                "submit",
                {
                    "form_id": "start_task",
                    "state": "planning",
                    "project": "alpha",
                    "runtime_profile_id": "strong_mcp_operator",
                    "payload": {
                        "project": "alpha",
                        "task_id": "task-simple-start",
                        "owner_agent": "codex",
                        "session_id": "sess-simple-start",
                        "agent_fingerprint": "agentfp:simple-start",
                        "auto_heartbeat": False,
                    },
                },
                "http://test",
            )

            data = json.loads(result)
            assert data["receipt"]["status"] == "started"
            assert data["receipt"]["work_token"]
            assert data["receipt"]["next_state"] == "implementation"
            assert {"record_progress", "finish_task"} <= set(data["receipt"]["next_forms"])
        finally:
            lease_store.close()
            stenographer_store.close()

    async def test_simple_submit_start_task_reclaims_expired_same_fingerprint_lease(self, monkeypatch):
        from datetime import timedelta

        from app.services import stenographer_service as stenographer_mod
        from app.services import task_lease_service as lease_mod

        lease_store = lease_mod.TaskLeaseStore(Path(":memory:"))
        stenographer_store = stenographer_mod.StenographerStore(Path(":memory:"))
        monkeypatch.setattr(lease_mod, "_STORE", lease_store)
        monkeypatch.setattr(stenographer_mod, "_STORE", stenographer_store)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        async def fake_post(api_base: str, path: str, payload: dict):
            return {"id": f"checkpoint-{payload.get('stage')}", **payload}

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        payload = {
            "project": "alpha",
            "task_id": "task-simple-ttl-reclaim",
            "owner_agent": "codex",
            "agent_fingerprint": "agentfp:simple-ttl-reclaim",
            "auto_heartbeat": False,
            "lease_ttl_seconds": 5,
        }
        try:
            started = json.loads(
                await mcp_sse._execute_tool(
                    "submit",
                    {
                        "form_id": "start_task",
                        "state": "planning",
                        "project": "alpha",
                        "payload": payload,
                    },
                    "http://test",
                )
            )
            first_lease = lease_store.get_active_claim(project="alpha", task_id=payload["task_id"])
            assert first_lease is not None
            lease_store.expire_stale(now=first_lease.expires_at + timedelta(seconds=1))

            reclaimed = json.loads(
                await mcp_sse._execute_tool(
                    "submit",
                    {
                        "form_id": "start_task",
                        "state": "planning",
                        "project": "alpha",
                        "payload": payload,
                    },
                    "http://test",
                )
            )

            assert reclaimed["receipt"]["status"] == "reclaimed_after_ttl"
            assert reclaimed["receipt"]["work_token"]
            assert reclaimed["receipt"]["work_token"] != started["receipt"]["work_token"]
            assert reclaimed["receipt"]["work_session_resumed"] is True
            assert reclaimed["receipt"]["work_session"]["work_id"] == started["receipt"]["work_session"]["work_id"]
            assert reclaimed["receipt"]["reclaim"] == {
                "same_fingerprint": True,
                "reason": "previous_lease_expired",
                "previous_lease_id": first_lease.lease_id,
                "previous_status": "expired",
            }
        finally:
            lease_store.close()
            stenographer_store.close()

    async def test_mailbox_get_returns_public_state_packet_by_ref(self):
        result = await mcp_sse._execute_tool(
            "mailbox_get",
            {
                "ref": "mailbox_state:alpha:verification",
                "runtime_profile_id": "weak_mcp_operator",
                "diagnostic": True,
            },
            "http://test",
        )

        data = json.loads(result)
        assert data["state"] == "verification"
        assert data["project"] == "alpha"
        assert "_internal" not in data
        assert any(form["form_id"] == "run_verification" for form in data["forms"])

    async def test_mailbox_submit_get_task_context_executes_public_read_form(self, monkeypatch):
        async def fake_pull_context(api_base: str, args: dict):
            assert args["project"] == "alpha"
            assert args["task_id"] == "task-123"
            assert args["detail"] == "compact"
            return {
                "status": "ready",
                "task_id": "task-123",
                "title": "Make weak-model workflow safe.",
                "next_safe_action": "Review context before claiming.",
            }

        monkeypatch.setattr(mcp_sse, "_build_pull_task_context_payload", fake_pull_context)
        result = await mcp_sse._execute_tool(
            "mailbox_submit",
            {
                "form_id": "get_task_context",
                "state": "planning",
                "project": "alpha",
                "payload": {"project": "alpha", "task_id": "task-123", "detail": "compact"},
            },
            "http://test",
        )

        data = json.loads(result)
        assert data["receipt"]["status"] == "accepted"
        assert data["receipt"]["data_ref"] == "task:alpha:task-123"
        assert data["result"]["title"] == "Make weak-model workflow safe."
        assert data["next_safe_action"] == "Review context before claiming."

    async def test_mailbox_get_task_reference_recovers_weak_model_ref(self, monkeypatch):
        async def fake_pull_context(api_base: str, args: dict):
            assert args["project"] == "alpha"
            assert args["task_id"] == "task-123"
            return {
                "status": "ready",
                "task_id": "task-123",
                "title": "Make weak-model workflow safe.",
                "next_safe_action": "Review context before claiming.",
            }

        monkeypatch.setattr(mcp_sse, "_build_pull_task_context_payload", fake_pull_context)
        result = await mcp_sse._execute_tool(
            "mailbox_get",
            {"ref": "mailbox_get:task:alpha:task-123", "state": "planning"},
            "http://test",
        )

        data = json.loads(result)
        assert data["receipt"]["status"] == "accepted"
        assert data["receipt"]["data_ref"] == "task:alpha:task-123"
        assert data["receipt"]["requested_ref"] == "mailbox_get:task:alpha:task-123"
        assert data["result"]["title"] == "Make weak-model workflow safe."

    async def test_mailbox_get_resolves_generic_public_artifact_ref(self, monkeypatch):
        requested: list[str] = []

        async def fake_get(api_base: str, path: str):
            requested.append(path)
            return {
                "artifact_key": "improvement:alpha:imp-123",
                "type": "improvement",
                "project": "alpha",
                "title": "Improve mailbox ref resolution.",
            }

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        result = await mcp_sse._execute_tool(
            "mailbox_get",
            {"ref": "mailbox_get:improvement:alpha:imp-123", "state": "planning"},
            "http://test",
        )

        data = json.loads(result)
        assert requested == ["/artifacts/improvement%3Aalpha%3Aimp-123"]
        assert data["receipt"]["status"] == "accepted"
        assert data["receipt"]["resource_kind"] == "improvement"
        assert data["receipt"]["data_ref"] == "improvement:alpha:imp-123"
        assert data["result"]["title"] == "Improve mailbox ref resolution."

    async def test_mailbox_get_unsupported_public_ref_kind_stays_in_workflow(self):
        result = await mcp_sse._execute_tool(
            "mailbox_get",
            {"ref": "mailbox_get:skill:alpha:skill-123", "state": "planning"},
            "http://test",
        )

        data = json.loads(result)
        assert data["receipt"]["status"] == "unsupported_ref_kind"
        assert data["receipt"]["data_ref"] == "skill:alpha:skill-123"
        assert "mailbox_state" in data["receipt"]["next_safe_action"]

    async def test_mailbox_get_resolves_governance_and_memory_refs(self, monkeypatch):
        requested: list[str] = []

        async def fake_get(api_base: str, path: str):
            requested.append(path)
            if path == "/laws/law-123":
                return {"id": "law-123", "title": "Use mailbox refs.", "status": "active"}
            if path == "/laws/candidates/candidate-123":
                return {"candidate_id": "candidate-123", "statement": "Route through mailbox.", "status": "trial"}
            if path == "/memories/mem-123":
                return {"id": "mem-123", "content": "Weak models need safe data reads."}
            raise AssertionError(path)

        monkeypatch.setattr(mcp_sse, "_get", fake_get)

        law = json.loads(
            await mcp_sse._execute_tool(
                "mailbox_get",
                {"ref": "mailbox_get:law:alpha:law-123", "state": "planning"},
                "http://test",
            )
        )
        candidate = json.loads(
            await mcp_sse._execute_tool(
                "mailbox_get",
                {"ref": "mailbox_get:rule_candidate:alpha:candidate-123", "state": "planning"},
                "http://test",
            )
        )
        memory = json.loads(
            await mcp_sse._execute_tool(
                "mailbox_get",
                {"ref": "mailbox_get:memory:alpha:mem-123", "state": "planning"},
                "http://test",
            )
        )

        assert requested == ["/laws/law-123", "/laws/candidates/candidate-123", "/memories/mem-123"]
        assert law["receipt"]["resource_kind"] == "law"
        assert candidate["receipt"]["resource_kind"] == "rule_candidate"
        assert memory["receipt"]["resource_kind"] == "memory"
        assert law["result"]["title"] == "Use mailbox refs."
        assert candidate["result"]["statement"] == "Route through mailbox."
        assert memory["result"]["content"] == "Weak models need safe data reads."

    async def test_simple_get_resolves_short_memory_ref_from_sqlite_store(self, monkeypatch):
        from app.services.memory_store import get_memory_store

        full_id = "0211928f-7238-4214-9ec9-ad99ff1fdfd0"
        await get_memory_store().upsert(
            full_id,
            "memory",
            "Root cause analysis: agent jumped to implementation without presenting plan.",
            {
                "project": "mnemoforge",
                "memory_type": "experience",
                "tags": ["workflow", "guardrail"],
                "source": "mailbox_submit.store_memory",
            },
        )
        requested: list[str] = []

        async def fake_get(api_base: str, path: str):
            requested.append(path)
            if path == f"/memories/{full_id}":
                return {
                    "id": full_id,
                    "content": "Root cause analysis: agent jumped to implementation without presenting plan.",
                    "memory_type": "experience",
                    "category": "memory",
                    "project": "mnemoforge",
                }
            raise AssertionError(path)

        monkeypatch.setattr(mcp_sse, "_get", fake_get)

        result = json.loads(
            await mcp_sse._execute_tool(
                "get",
                {"project": "mnemoforge", "ref": "memory:mnemoforge:0211928f", "state": "planning"},
                "http://test",
            )
        )

        assert result["receipt"]["status"] == "accepted"
        assert result["receipt"]["resource_kind"] == "memory"
        assert result["receipt"]["data_ref"] == f"memory:mnemoforge:{full_id}"
        assert result["receipt"]["ref_source"] == "memory_store_prefix"
        assert result["result"]["id"] == full_id
        assert requested == [f"/memories/{full_id}"]

    async def test_simple_get_find_memory_short_id_beats_task_id_lookup(self, monkeypatch):
        from app.services.memory_store import get_memory_store

        full_id = "0211928f-7238-4214-9ec9-ad99ff1fdfd0"
        await get_memory_store().upsert(
            full_id,
            "memory",
            "Root cause analysis: agent jumped to implementation without presenting plan.",
            {"project": "mnemoforge", "memory_type": "experience"},
        )

        async def fake_get(api_base: str, path: str):
            if path == f"/memories/{full_id}":
                return {
                    "id": full_id,
                    "content": "Root cause analysis: agent jumped to implementation without presenting plan.",
                    "memory_type": "experience",
                    "category": "memory",
                    "project": "mnemoforge",
                }
            raise AssertionError(path)

        async def forbidden_post(api_base: str, path: str, payload: dict):
            raise AssertionError("explicit memory id lookup should not fall back to semantic memory search")

        async def forbidden_ask_project(api_base: str, args: dict, *, session_id: str | None = None):
            raise AssertionError("explicit memory id lookup should not route through task/project lookup")

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        monkeypatch.setattr(mcp_sse, "_post", forbidden_post)
        monkeypatch.setattr(mcp_sse, "_build_ask_project_payload", forbidden_ask_project)

        result = json.loads(
            await mcp_sse._execute_tool(
                "get",
                {"project": "mnemoforge", "query": "find memory 0211928f", "state": "planning", "diagnostic": True},
                "http://test",
            )
        )

        assert result["receipt"]["status"] == "accepted"
        assert result["receipt"]["resource_kind"] == "memory"
        assert result["simple_interface"]["route"] == "memory_ref_lookup"
        assert result["result"]["id"] == full_id

    async def test_simple_get_resolves_short_improvement_refs(self, monkeypatch):
        from app.services.public_ref_index import get_public_ref_index_store

        get_public_ref_index_store().clear()
        requested: list[str] = []

        async def fake_get(api_base: str, path: str):
            requested.append(path)
            if path == "/artifacts/improvement%3Aalpha%3Aebcf2a91":
                raise httpx.HTTPStatusError("not found", request=httpx.Request("GET", "http://test"), response=httpx.Response(404))
            if path == "/artifacts?project=alpha&type=improvement&limit=100":
                raise httpx.HTTPStatusError("unsupported type", request=httpx.Request("GET", "http://test"), response=httpx.Response(422))
            if path == "/artifacts?project=alpha&limit=100":
                return {
                    "items": [
                        {
                            "artifact_key": "task:alpha:ebcf2a91-bcf6-4072-bfed-99b37c990a48",
                            "linked_artifact_key": "improvement:alpha:ebcf2a91-bcf6-4072-bfed-99b37c990a48",
                            "type": "task",
                            "task_id": "ebcf2a91-bcf6-4072-bfed-99b37c990a48",
                            "title": "Batch 6 finish_task conflict",
                        }
                    ]
                }
            if path == "/artifacts/improvement%3Aalpha%3Aebcf2a91-bcf6-4072-bfed-99b37c990a48":
                return {
                    "artifact_key": "improvement:alpha:ebcf2a91-bcf6-4072-bfed-99b37c990a48",
                    "title": "Batch 6 finish_task conflict",
                    "status": "open",
                }
            raise AssertionError(path)

        monkeypatch.setattr(mcp_sse, "_get", fake_get)

        result = await mcp_sse._execute_tool(
            "get",
            {"project": "alpha", "ref": "improvement:alpha:ebcf2a91", "state": "planning"},
            "http://test",
        )
        data = json.loads(result)

        assert data["receipt"]["status"] == "accepted"
        assert data["receipt"]["data_ref"] == "improvement:alpha:ebcf2a91-bcf6-4072-bfed-99b37c990a48"
        assert data["receipt"]["requested_ref"] == "improvement:alpha:ebcf2a91"
        assert data["result"]["title"] == "Batch 6 finish_task conflict"
        assert requested == [
            "/artifacts/improvement%3Aalpha%3Aebcf2a91",
            "/artifacts?project=alpha&type=improvement&limit=100",
            "/artifacts?project=alpha&limit=100",
            "/artifacts/improvement%3Aalpha%3Aebcf2a91-bcf6-4072-bfed-99b37c990a48",
        ]

    async def test_simple_get_short_ref_uses_index_before_list_fallback(self, monkeypatch):
        from app.services.public_ref_index import get_public_ref_index_store

        store = get_public_ref_index_store()
        store.clear()
        full_id = "de609b8f-462f-4bf0-a0fb-6f40c51b54b7"
        store.upsert_artifact(
            {
                "artifact_key": f"improvement:alpha:{full_id}",
                "linked_artifact_key": f"task:alpha:{full_id}",
                "title": "Indexed task",
                "status": "open",
            }
        )
        requested: list[str] = []

        async def fake_get(api_base: str, path: str):
            requested.append(path)
            if path == "/artifacts/improvement%3Aalpha%3Ade609b8f":
                raise httpx.HTTPStatusError("not found", request=httpx.Request("GET", "http://test"), response=httpx.Response(404))
            if path == f"/artifacts/improvement%3Aalpha%3A{full_id}":
                return {
                    "artifact_key": f"improvement:alpha:{full_id}",
                    "linked_artifact_key": f"task:alpha:{full_id}",
                    "title": "Indexed task",
                    "status": "open",
                }
            if path.startswith("/artifacts?"):
                raise AssertionError("short ref should not scan artifacts when index is valid")
            raise AssertionError(path)

        monkeypatch.setattr(mcp_sse, "_get", fake_get)

        result = await mcp_sse._execute_tool(
            "get",
            {"project": "alpha", "ref": "improvement:alpha:de609b8f", "state": "planning"},
            "http://test",
        )
        data = json.loads(result)

        assert data["receipt"]["status"] == "accepted"
        assert data["receipt"]["data_ref"] == f"improvement:alpha:{full_id}"
        assert data["receipt"]["ref_source"] == "public_ref_index"
        assert requested == [
            "/artifacts/improvement%3Aalpha%3Ade609b8f",
            f"/artifacts/improvement%3Aalpha%3A{full_id}",
            f"/artifacts/improvement%3Aalpha%3A{full_id}",
        ]

    async def test_simple_help_state_get_submit_aliases_wrap_mailbox_surface(self, monkeypatch):
        async def fake_pull_context(api_base: str, args: dict):
            assert args["project"] == "alpha"
            assert args["task_id"] == "task-123"
            return {
                "status": "ready",
                "task_id": "task-123",
                "title": "Use simple mailbox aliases.",
                "next_safe_action": "Review context before claiming.",
            }

        monkeypatch.setattr(mcp_sse, "_build_pull_task_context_payload", fake_pull_context)

        help_result = json.loads(
            await mcp_sse._execute_tool(
                "help",
                {"project": "alpha", "topic": "task context", "runtime_profile_id": "weak_mcp_operator"},
                "http://test",
            )
        )
        state_result = json.loads(
            await mcp_sse._execute_tool(
                "state",
                {"project": "alpha", "state": "planning", "runtime_profile_id": "weak_mcp_operator"},
                "http://test",
            )
        )
        get_result = json.loads(
            await mcp_sse._execute_tool(
                "get",
                {"ref": "task:alpha:task-123", "state": "planning"},
                "http://test",
            )
        )
        submit_result = json.loads(
            await mcp_sse._execute_tool(
                "submit",
                {
                    "project": "alpha",
                    "state": "planning",
                    "action": "get_task_context",
                    "payload": {"project": "alpha", "task_id": "task-123"},
                },
                "http://test",
            )
        )
        put_result = json.loads(
            await mcp_sse._execute_tool(
                "put",
                {
                    "project": "alpha",
                    "state": "planning",
                    "action": "get_task_context",
                    "payload": {"project": "alpha", "task_id": "task-123"},
                },
                "http://test",
            )
        )

        assert "simple_interface" not in help_result
        assert "stage" not in help_result
        assert "feedback_expected" not in help_result
        assert "follow_up" not in help_result
        assert "forms" not in help_result
        assert help_result["tools"]["state"].startswith("Current workflow")
        assert "simple_interface" not in state_result
        assert any(form["form_id"] == "get_task_context" for form in state_result["forms"])
        assert get_result["receipt"]["data_ref"] == "task:alpha:task-123"
        assert get_result["result"]["title"] == "Use simple mailbox aliases."
        assert "simple_interface" not in submit_result
        assert submit_result["result"]["title"] == "Use simple mailbox aliases."
        assert "simple_interface" not in put_result
        assert put_result["result"]["title"] == "Use simple mailbox aliases."

        diagnostic_help = json.loads(
            await mcp_sse._execute_tool(
                "help",
                {"project": "alpha", "diagnostic": True, "runtime_profile_id": "diagnostic_operator"},
                "http://test",
            )
        )
        assert diagnostic_help["simple_interface"]["tools"] == ["help", "state", "get", "submit"]
        assert diagnostic_help["stage"]

    async def test_simple_get_compacts_task_payload_by_default_and_full_keeps_details(self, monkeypatch):
        seen_args: list[dict] = []

        async def fake_pull_context(api_base: str, args: dict):
            seen_args.append(dict(args))
            return {
                "project": args["project"],
                "task_id": args["task_id"],
                "status": "ready",
                "task": {"title": "Compact task details.", "status": "planning"},
                "latest_checkpoint": {
                    "id": "checkpoint-1",
                    "stage": "implementation",
                    "status": "active",
                    "summary": "Implemented compact envelope.",
                    "changed_files": ["app/routers/mcp_sse.py"],
                    "verification": ["Docker passed."],
                },
                "available_layers": {"task_history": {"available": True}},
                "token_budget": {"estimated_tokens": 9000},
                "token_overhead": {"estimated_tokens": 9000},
                "recommended_first_tool": "record_task_checkpoint",
                "next_safe_action": "Use detail=full for full replay.",
            }

        monkeypatch.setattr(mcp_sse, "_build_pull_task_context_payload", fake_pull_context)
        compact = json.loads(
            await mcp_sse._execute_tool(
                "get",
                {"ref": "task:alpha:task-123", "state": "planning"},
                "http://test",
            )
        )
        full = json.loads(
            await mcp_sse._execute_tool(
                "get",
                {"ref": "task:alpha:task-123", "state": "planning", "detail": "full"},
                "http://test",
            )
        )

        assert compact["details_available"] is True
        assert compact["result"]["task_id"] == "task-123"
        assert compact["result"]["title"] == "Compact task details."
        assert compact["result"]["latest_checkpoint"]["summary"] == "Implemented compact envelope."
        assert compact["result"]["recommended_first_tool"] == "submit"
        assert compact["result"]["recommended_next_call"]["form_id"] == "record_progress"
        assert "internal_tool" not in compact["result"]["recommended_next_call"]
        assert "available_layers" not in compact["result"]
        assert "token_budget" not in compact["result"]
        assert full["result"]["available_layers"]["task_history"]["available"] is True
        assert full["result"]["token_budget"]["estimated_tokens"] == 9000
        assert seen_args[0]["agent_id"] == ""

    async def test_simple_get_task_ref_forwards_agent_identity_for_handoff_reads(self, monkeypatch):
        seen_args: list[dict] = []

        async def fake_pull_context(api_base: str, args: dict):
            seen_args.append(dict(args))
            return {
                "project": args["project"],
                "task_id": args["task_id"],
                "status": "ready",
                "task": {"title": "Handoff-aware task.", "status": "planning"},
            }

        monkeypatch.setattr(mcp_sse, "_build_pull_task_context_payload", fake_pull_context)
        tool = next(tool for tool in mcp_sse.TOOLS if tool["name"] == "get")

        result = json.loads(
            await mcp_sse._execute_tool(
                "get",
                {"ref": "task:alpha:task-123", "agent_id": "handoff-reader"},
                "http://test",
            )
        )

        assert result["result"]["task_id"] == "task-123"
        assert tool["inputSchema"]["properties"]["agent_id"]["type"] == "string"
        assert seen_args == [
            {
                "project": "alpha",
                "task_id": "task-123",
                "detail": "compact",
                "include_handoffs": True,
                "agent_id": "handoff-reader",
                "limit": 10,
            }
        ]

    async def test_simple_get_accepts_auto_response_format_for_weak_clients(self, monkeypatch):
        async def fake_pull_context(api_base: str, args: dict):
            return {
                "project": args["project"],
                "task_id": args["task_id"],
                "status": "ready",
                "task": {"title": "Auto format task.", "status": "planning"},
            }

        monkeypatch.setattr(mcp_sse, "_build_pull_task_context_payload", fake_pull_context)
        tool = next(tool for tool in mcp_sse.TOOLS if tool["name"] == "get")
        assert "auto" in tool["inputSchema"]["properties"]["response_format"]["enum"]

        result = json.loads(
            await mcp_sse._execute_tool(
                "get",
                {"ref": "task:alpha:task-123", "state": "planning", "response_format": "auto"},
                "http://test",
            )
        )

        assert result["receipt"]["status"] == "accepted"
        assert result["result"]["task_id"] == "task-123"
        assert result["result"]["title"] == "Auto format task."

    async def test_simple_get_query_uses_memory_search_for_generic_semantic_reads(self, monkeypatch):
        posted: list[tuple[str, dict]] = []

        async def fake_post(api_base: str, path: str, payload: dict):
            posted.append((path, payload))
            assert path == "/memories/search"
            return [
                {
                    "score": 0.91,
                    "memory": {
                        "id": "mem-1",
                        "content": "Stored fact about mailbox usability.",
                        "memory_type": "context",
                        "category": "mnemoforge:fact",
                        "project": "alpha",
                    },
                }
            ]

        async def forbidden_ask_project(api_base: str, args: dict, *, session_id: str | None = None):
            raise AssertionError("generic get(query) should not route through ask_project disambiguation")

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_build_ask_project_payload", forbidden_ask_project)

        result = json.loads(
            await mcp_sse._execute_tool(
                "get",
                {"project": "alpha", "query": "mailbox usability stored fact", "limit": 5, "diagnostic": True},
                "http://test",
            )
        )

        assert result["receipt"]["status"] == "accepted"
        assert result["receipt"]["resource_kind"] == "memory_search"
        assert result["simple_interface"]["route"] == "memory_search"
        assert result["result"][0]["id"] == "mem-1"
        assert posted[0][1]["project"] == "alpha"
        assert posted[0][1]["context_project"] == "alpha"

    async def test_simple_get_query_explicit_memory_search_beats_project_term_overlap(self, monkeypatch):
        posted: list[tuple[str, dict]] = []

        async def fake_post(api_base: str, path: str, payload: dict):
            posted.append((path, payload))
            assert path == "/memories/search"
            return []

        async def forbidden_ask_project(api_base: str, args: dict, *, session_id: str | None = None):
            raise AssertionError("explicit memory lookup should not route through the project expert")

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_build_ask_project_payload", forbidden_ask_project)

        result = json.loads(
            await mcp_sse._execute_tool(
                "get",
                {"project": "alpha", "query": "search memories for TTL reclaim receipt", "diagnostic": True},
                "http://test",
            )
        )

        assert result["receipt"]["resource_kind"] == "memory_search"
        assert result["simple_interface"]["route"] == "memory_search"
        assert posted[0][1]["query"] == "search memories for TTL reclaim receipt"

    async def test_simple_get_query_lists_improvements_via_artifact_surface(self, monkeypatch):
        seen: dict[str, str] = {}

        async def fake_get(api_base: str, path: str):
            seen["path"] = path
            return {
                "items": [
                    {
                        "artifact_key": "improvement:alpha:imp-1",
                        "type": "improvement",
                        "id": "imp-1",
                        "title": "Improve response profile",
                        "status": "open",
                        "linked_artifact_key": "task:alpha:imp-1",
                        "linked_status": "open",
                    }
                ]
            }

        async def forbidden_ask_project(api_base: str, args: dict, *, session_id: str | None = None):
            raise AssertionError("explicit improvement list should not route through ask_project")

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        monkeypatch.setattr(mcp_sse, "_build_ask_project_payload", forbidden_ask_project)

        result = json.loads(
            await mcp_sse._execute_tool(
                "get",
                {"project": "alpha", "query": "list recent improvements", "limit": 5},
                "http://test",
            )
        )

        assert seen["path"] == "/artifacts?project=alpha&limit=5&type=improvement"
        assert result["receipt"]["resource_kind"] == "artifact_list"
        assert result["receipt"]["artifact_type"] == "improvement"
        assert result["result"]["items"][0]["artifact_key"] == "improvement:alpha:imp-1"
        assert result["result"]["items"][0]["type"] == "improvement"
        assert "simple_interface" not in result

    async def test_simple_get_query_lists_mixed_tasks_and_improvements(self, monkeypatch):
        seen: dict[str, str] = {}

        async def fake_get(api_base: str, path: str):
            seen["path"] = path
            return {
                "items": [
                    {"artifact_key": "task:alpha:task-1", "type": "task", "task_id": "task-1", "title": "Task", "status": "open"},
                    {"artifact_key": "improvement:alpha:imp-1", "type": "improvement", "id": "imp-1", "title": "Improvement", "status": "open"},
                ]
            }

        async def forbidden_ask_project(api_base: str, args: dict, *, session_id: str | None = None):
            raise AssertionError("mixed work item list should use the artifact surface directly")

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        monkeypatch.setattr(mcp_sse, "_build_ask_project_payload", forbidden_ask_project)

        result = json.loads(
            await mcp_sse._execute_tool(
                "get",
                {"project": "alpha", "query": "list tasks and improvements", "limit": 5},
                "http://test",
            )
        )

        assert seen["path"] == "/artifacts?project=alpha&limit=5"
        assert result["receipt"]["resource_kind"] == "artifact_list"
        assert result["receipt"]["artifact_type"] == "all"
        assert [item["type"] for item in result["result"]["items"]] == ["task", "improvement"]

    async def test_simple_get_query_keeps_project_questions_on_project_expert(self, monkeypatch):
        async def fake_ask_project(api_base: str, args: dict, *, session_id: str | None = None):
            return {
                "facade": "ask_project",
                "question": args["question"],
                "selected_expert_route": {"facade": "project_work"},
                "result_text": "Mnemoforge answer\nAnswer: project_work executed route list_open_tasks.",
            }

        async def forbidden_post(api_base: str, path: str, payload: dict):
            raise AssertionError("project task query should not route to memory_search")

        monkeypatch.setattr(mcp_sse, "_build_ask_project_payload", fake_ask_project)
        monkeypatch.setattr(mcp_sse, "_post", forbidden_post)

        result = json.loads(
            await mcp_sse._execute_tool(
                "get",
                {"project": "alpha", "query": "list active tasks", "limit": 5},
                "http://test",
            )
        )

        assert result["receipt"]["status"] == "accepted"
        assert result["receipt"]["resource_kind"] == "query"
        assert "simple_interface" not in result
        assert result["result"]["selected_facade"] == "project_work"

    async def test_simple_get_project_query_requires_project_scope(self, monkeypatch):
        async def forbidden_ask_project(api_base: str, args: dict, *, session_id: str | None = None):
            raise AssertionError("project-scoped query without project must not fall back to mnemoforge")

        async def forbidden_post(api_base: str, path: str, payload: dict):
            raise AssertionError("project-scoped query without project must not become a memory search")

        monkeypatch.setattr(mcp_sse, "_build_ask_project_payload", forbidden_ask_project)
        monkeypatch.setattr(mcp_sse, "_post", forbidden_post)

        result = json.loads(
            await mcp_sse._execute_tool(
                "get",
                {"query": "list active tasks", "limit": 5},
                "http://test",
            )
        )

        assert result["receipt"]["status"] == "needs_project"
        assert result["receipt"]["missing_fields"] == ["project"]
        assert "project" not in result
        assert "mnemoforge" not in json.dumps(result)

    async def test_simple_get_project_query_uses_session_project_scope(self, monkeypatch):
        seen: list[dict] = []

        async def fake_identity(session_id: str | None):
            assert session_id == "session-alpha"
            return {"project_id": "alpha", "runtime_profile_id": "weak_mcp_operator"}

        async def fake_ask_project(api_base: str, args: dict, *, session_id: str | None = None):
            seen.append(dict(args))
            return {
                "facade": "ask_project",
                "question": args["question"],
                "selected_expert_route": {"facade": "project_verify"},
                "result_text": json.dumps(
                    {
                        "status": "executed",
                        "selected_route": {"tool": "get_task_execution_context", "intent_type": "verification_context"},
                        "result": {"project": args["project"], "status": "ok"},
                    }
                ),
            }

        monkeypatch.setattr(mcp_sse, "_get_session_identity_defaults", fake_identity)
        monkeypatch.setattr(mcp_sse, "_build_ask_project_payload", fake_ask_project)

        result = json.loads(
            await mcp_sse._execute_tool(
                "get",
                {"query": "verify server functionality", "limit": 5},
                "http://test",
                session_id="session-alpha",
            )
        )

        assert result["project"] == "alpha"
        assert seen[0]["project"] == "alpha"
        assert result["result"]["selected_facade"] == "project_verify"

    async def test_simple_get_explicit_project_can_cross_session_project_scope(self, monkeypatch):
        seen: list[dict] = []

        async def fake_identity(session_id: str | None):
            return {"project_id": "frontend", "runtime_profile_id": "weak_mcp_operator"}

        async def fake_ask_project(api_base: str, args: dict, *, session_id: str | None = None):
            seen.append(dict(args))
            return {
                "facade": "ask_project",
                "question": args["question"],
                "selected_expert_route": {"facade": "project_work"},
                "result_text": json.dumps(
                    {
                        "status": "executed",
                        "selected_route": {"tool": "list_open_tasks", "intent_type": "next_priority"},
                        "compact_result": [{"task_id": "backend-task", "title": "Backend issue", "status": "open"}],
                    }
                ),
            }

        monkeypatch.setattr(mcp_sse, "_get_session_identity_defaults", fake_identity)
        monkeypatch.setattr(mcp_sse, "_build_ask_project_payload", fake_ask_project)

        result = json.loads(
            await mcp_sse._execute_tool(
                "get",
                {"project": "backend", "query": "list active tasks", "limit": 5},
                "http://test",
                session_id="session-frontend",
            )
        )

        assert result["project"] == "backend"
        assert seen[0]["project"] == "backend"
        assert result["result"]["items"][0]["task_id"] == "backend-task"

    async def test_simple_get_query_compacts_project_expert_json_text_for_weak_models(self, monkeypatch):
        async def fake_ask_project(api_base: str, args: dict, *, session_id: str | None = None):
            return {
                "facade": "ask_project",
                "question": args["question"],
                "selected_expert_route": {"facade": "project_work"},
                "result_text": json.dumps(
                    {
                        "status": "executed",
                        "action_status": "executed",
                        "selected_route": {
                            "tool": "list_open_tasks",
                            "family": "project_knowledge",
                            "intent_type": "next_priority",
                            "confidence": 0.88,
                            "route_candidates": [{"tool": "verbose_internal_candidate"}],
                        },
                        "agent_action": {
                            "one_sentence_summary": "project_work selected list_open_tasks."
                        },
                        "compact_result": [
                            {"task_id": "task-1", "title": "First task", "status": "open"},
                            {"task_id": "task-2", "title": "Second task", "status": "open"},
                        ],
                        "route_telemetry": {"debug": "hidden in compact public get"},
                        "result": {"items": [{"task_id": "task-1"}, {"task_id": "task-2"}]},
                        "next_safe_action": "Choose a returned task ref for details.",
                    }
                ),
            }

        monkeypatch.setattr(mcp_sse, "_build_ask_project_payload", fake_ask_project)

        result = json.loads(
            await mcp_sse._execute_tool(
                "get",
                {"project": "alpha", "query": "list active tasks", "limit": 5},
                "http://test",
            )
        )

        payload = result["result"]
        assert payload["selected_facade"] == "project_work"
        assert payload["items"][0]["task_id"] == "task-1"
        assert "selected_route" not in payload
        assert "answer" not in payload
        assert "route_telemetry" not in payload
        assert "route_candidates" not in json.dumps(payload)

    async def test_simple_state_compacts_forms_by_default_and_full_keeps_schemas(self):
        compact = json.loads(
            await mcp_sse._execute_tool(
                "state",
                {"project": "alpha", "state": "planning"},
                "http://test",
            )
        )
        full = json.loads(
            await mcp_sse._execute_tool(
                "state",
                {"project": "alpha", "state": "planning", "detail": "full"},
                "http://test",
            )
        )

        assert compact["details_available"] is True
        assert "input_schema" not in compact["forms"][0]
        assert {"form_id", "title", "mode", "required_fields"} <= set(compact["forms"][0])
        assert "input_schema" in full["forms"][0]

    async def test_simple_state_minimal_profile_returns_short_adaptive_packet(self):
        compact = json.loads(
            await mcp_sse._execute_tool(
                "state",
                {"project": "alpha", "state": "planning", "runtime_profile_id": "weak_mcp_operator"},
                "http://test",
            )
        )
        full = json.loads(
            await mcp_sse._execute_tool(
                "state",
                {
                    "project": "alpha",
                    "state": "planning",
                    "runtime_profile_id": "weak_mcp_operator",
                    "detail": "full",
                },
                "http://test",
            )
        )

        assert len(compact["forms"]) == 5
        assert compact["packet_profile"] == "minimal"
        assert "record_progress" in compact["hidden_forms"]
        assert "omitted_forms" in compact
        assert len(full["forms"]) > len(compact["forms"])
        assert "input_schema" in full["forms"][0]

    async def test_simple_submit_compacts_task_shaped_result_by_default_and_full_keeps_details(self, monkeypatch):
        seen_details: list[str] = []

        async def fake_pull_context(api_base: str, args: dict):
            seen_details.append(str(args.get("detail") or ""))
            return {
                "project": args["project"],
                "task_id": args["task_id"],
                "status": "ready",
                "task": {"title": "Compact put task.", "status": "planning"},
                "latest_checkpoint": {"id": "checkpoint-1", "summary": "Latest compact checkpoint."},
                "available_layers": {"task_history": {"available": True}},
                "token_budget": {"estimated_tokens": 9000},
                "next_safe_action": "Use compact receipt first.",
            }

        monkeypatch.setattr(mcp_sse, "_build_pull_task_context_payload", fake_pull_context)
        args = {
            "project": "alpha",
            "state": "planning",
            "form_id": "get_task_context",
            "payload": {"project": "alpha", "task_id": "task-123"},
        }
        compact = json.loads(await mcp_sse._execute_tool("submit", args, "http://test"))
        full = json.loads(await mcp_sse._execute_tool("submit", {**args, "detail": "full"}, "http://test"))

        assert compact["details_available"] is True
        assert compact["receipt"]["status"] == "accepted"
        assert "simple_interface" not in compact
        assert compact["result"]["task_id"] == "task-123"
        assert compact["result"]["title"] == "Compact put task."
        assert "available_layers" not in compact["result"]
        assert "token_budget" not in compact["result"]
        assert full["result"]["available_layers"]["task_history"]["available"] is True
        assert full["result"]["token_budget"]["estimated_tokens"] == 9000
        assert seen_details == ["compact", "full"]

    async def test_operational_tray_inspect_posts_context_request(self, monkeypatch):
        posted: list[tuple[str, dict]] = []

        async def fake_post(api_base: str, path: str, payload: dict):
            posted.append((path, payload))
            return {
                "project": payload["project"],
                "state": payload["state"],
                "task": payload["task"],
                "readiness": {"ready_to_enter": True},
                "operation_tray": {"primary_tools": ["record_task_checkpoint"]},
                "required_rules": [],
                "recommended_rules": [],
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        result = await mcp_sse._execute_tool(
            "operational_tray",
            {
                "project": "alpha",
                "task_id": "task-1",
                "task": "Inspect current tray.",
                "state": "implementation",
                "action": "inspect",
                "stage_evidence": ["checkpoint:planning-1"],
            },
            "http://test",
        )

        assert posted == [
            (
                "/task-execution-context",
                {
                    "project": "alpha",
                    "task_id": "task-1",
                    "task": "Inspect current tray.",
                    "state": "implementation",
                    "intent": "",
                    "changed_files": [],
                    "stage_evidence": ["checkpoint:planning-1"],
                    "include_tools": True,
                    "include_rules": True,
                },
            )
        ]
        assert '"catalog_hidden": true' in result
        assert '"stage": "testing"' in result or '"stage": "stable"' in result

    async def test_operational_tray_inspect_returns_compact_rule_packet(self, monkeypatch):
        async def fake_post(api_base: str, path: str, payload: dict):
            return {
                "project": payload["project"],
                "state": payload["state"],
                "task": payload["task"],
                "readiness": {"ready_to_enter": True},
                "operation_tray": {"primary_tools": ["record_task_checkpoint"]},
                "required_rules": [
                    {"id": "r1", "title": "Use Docker contour", "reason": "Do not use host pytest for verification."}
                ],
                "recommended_rules": [
                    {"id": "r2", "title": "Record checkpoints", "reason": "Persist stage transitions."}
                ],
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        text = await mcp_sse._execute_tool(
            "operational_tray",
            {
                "project": "alpha",
                "task_id": "task-1",
                "task": "Run tests and verify changes.",
                "state": "verification",
                "intent": "run tests with docker contour",
                "action": "inspect",
                "detail": "compact",
                "relevance_threshold": 0.1,
                "top_rules_limit": 1,
            },
            "http://test",
        )
        data = json.loads(text)
        packet = data["rule_packet"]
        assert packet["detail"] == "compact"
        assert packet["applied_rules"][0]["id"] == "r1"
        assert "available_rules" in packet
        assert "available_rules_full" not in packet

    async def test_operational_tray_rule_packet_scores_unicode_db_terms(self, monkeypatch):
        async def fake_post(api_base: str, path: str, payload: dict):
            return {
                "project": payload["project"],
                "state": payload["state"],
                "task": payload["task"],
                "readiness": {"ready_to_enter": True},
                "operation_tray": {"primary_tools": ["record_task_checkpoint"]},
                "required_rules": [],
                "recommended_rules": [
                    {"id": "r-unicode", "title": "规则 轮廓", "reason": "项目 uses learned multilingual rule terms."}
                ],
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        text = await mcp_sse._execute_tool(
            "operational_tray",
            {
                "project": "alpha",
                "task_id": "task-1",
                "task": "规则 validation",
                "state": "verification",
                "intent": "规则 轮廓",
                "action": "inspect",
                "detail": "compact",
                "relevance_threshold": 0.1,
            },
            "http://test",
        )
        data = json.loads(text)
        assert data["rule_packet"]["applied_rules"][0]["id"] == "r-unicode"
        assert data["rule_packet"]["applied_rules"][0]["relevance_score"] > 0

    async def test_operational_tray_inspect_rule_id_returns_selected_rule(self, monkeypatch):
        async def fake_post(api_base: str, path: str, payload: dict):
            return {
                "project": payload["project"],
                "state": payload["state"],
                "task": payload["task"],
                "readiness": {"ready_to_enter": True},
                "operation_tray": {"primary_tools": ["record_task_checkpoint"]},
                "required_rules": [],
                "recommended_rules": [
                    {"id": "r2", "title": "Record checkpoints", "reason": "Persist stage transitions."}
                ],
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        text = await mcp_sse._execute_tool(
            "operational_tray",
            {
                "project": "alpha",
                "task_id": "task-1",
                "task": "Inspect tray.",
                "state": "implementation",
                "action": "inspect",
                "detail": "full",
                "rule_id": "r2",
            },
            "http://test",
        )
        data = json.loads(text)
        assert data["rule_packet"]["selected_rule"]["id"] == "r2"
        assert "available_rules_full" in data["rule_packet"]

    async def test_operational_tray_blocks_non_evidence_action_when_not_ready(self, monkeypatch):
        posted: list[tuple[str, dict]] = []

        async def fake_post(api_base: str, path: str, payload: dict):
            posted.append((path, payload))
            return {
                "project": payload["project"],
                "state": payload["state"],
                "task": payload["task"],
                "readiness": {
                    "ready_to_enter": False,
                    "missing_prerequisites": ["task_framing_not_recorded"],
                },
                "operation_tray": {"primary_tools": ["record_task_checkpoint"]},
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        result = await mcp_sse._execute_tool(
            "operational_tray",
            {
                "project": "alpha",
                "task_id": "task-1",
                "task": "Review rule candidates.",
                "state": "implementation",
                "action": "execute",
                "tray_action": "review_rule_candidates",
            },
            "http://test",
        )

        assert posted[0][0] == "/task-execution-context"
        assert len(posted) == 1
        assert '"blocked": true' in result
        assert "task_framing_not_recorded" in result

    async def test_operational_tray_executes_record_stage_evidence(self, monkeypatch):
        from pathlib import Path
        from app.services import task_lease_service as lease_mod

        posted: list[tuple[str, dict]] = []

        async def fake_post(api_base: str, path: str, payload: dict):
            posted.append((path, payload))
            if path == "/task-execution-context":
                return {
                    "project": payload["project"],
                    "state": payload["state"],
                    "task": payload["task"],
                    "readiness": {
                        "ready_to_enter": False,
                        "missing_prerequisites": ["task_framing_not_recorded"],
                    },
                    "operation_tray": {"primary_tools": ["record_task_checkpoint"]},
                }
            return {"id": "change-1", "task_id": "task-1", "stage": "planning", "status": "planning"}

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())
        lease_store = lease_mod.TaskLeaseStore(Path(":memory:"))
        monkeypatch.setattr(lease_mod, "_STORE", lease_store)
        try:
            claimed = await mcp_sse._execute_tool(
                "claim_task",
                {"project": "alpha", "task_id": "task-1", "owner_agent": "codex", "session_id": "sess-1"},
                "http://test",
            )
            claimed_data = json.loads(claimed)
            work_token = claimed_data.get("work_token", "")
            result = await mcp_sse._execute_tool(
                "operational_tray",
                {
                    "project": "alpha",
                    "task_id": "task-1",
                    "task": "Record framing evidence.",
                    "state": "implementation",
                    "action": "execute",
                    "tray_action": "record_stage_evidence",
                    "work_token": work_token,
                    "args": {
                        "stage": "planning",
                        "summary": "Task framing recorded.",
                        "session_id": "sess-1",
                    },
                },
                "http://test",
            )

            assert posted[0][0] == "/task-execution-context"
            change_post = next(item for item in posted if item[0] == "/project/tasks/task-1/changes")
            assert "checkpoint_mode:lightweight" in change_post[1]["tags"]
            assert "Checkpoint recorded for task task-1" in result
            assert "stage_evidence=checkpoint:change-1" in result
        finally:
            lease_store.close()

    async def test_operational_tray_executes_checkpoint_with_documentation_state_aliases(self, monkeypatch):
        from pathlib import Path
        from app.services import task_lease_service as lease_mod

        posted: list[tuple[str, dict]] = []

        async def fake_post(api_base: str, path: str, payload: dict):
            posted.append((path, payload))
            if path == "/task-execution-context":
                return {
                    "project": payload["project"],
                    "state": payload["state"],
                    "task": payload["task"],
                    "readiness": {"ready_to_enter": True},
                    "operation_tray": {"primary_tools": ["record_task_checkpoint"]},
                }
            return {"id": "change-doc-1", "task_id": "task-1", "stage": "completed", "status": "done"}

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())
        lease_store = lease_mod.TaskLeaseStore(Path(":memory:"))
        monkeypatch.setattr(lease_mod, "_STORE", lease_store)
        try:
            claimed = await mcp_sse._execute_tool(
                "claim_task",
                {"project": "alpha", "task_id": "task-1", "owner_agent": "codex", "session_id": "sess-1"},
                "http://test",
            )
            claimed_data = json.loads(claimed)
            work_token = claimed_data.get("work_token", "")
            result = await mcp_sse._execute_tool(
                "operational_tray",
                {
                    "project": "alpha",
                    "task_id": "task-1",
                    "task": "Record documentation stage evidence.",
                    "state": "documentation",
                    "action": "execute",
                    "tool": "record_checkpoint",
                    "work_token": work_token,
                    "arguments": {
                        "stage": "completed",
                        "summary": "Documentation projection knowledge recorded.",
                        "status": "done",
                        "session_id": "sess-1",
                    },
                    "stage_evidence": ["checkpoint:planning-1"],
                    "prior_stage_recorded": True,
                },
                "http://test",
            )

            assert posted[0] == (
                "/task-execution-context",
                {
                    "project": "alpha",
                    "task_id": "task-1",
                    "task": "Record documentation stage evidence.",
                    "state": "documentation",
                    "intent": "",
                    "changed_files": [],
                    "stage_evidence": ["checkpoint:planning-1"],
                    "include_tools": True,
                    "include_rules": True,
                    "prior_stage_recorded": True,
                },
            )
            change_post = next(item for item in posted if item[0] == "/project/tasks/task-1/changes")
            assert "task_stage:completed" in change_post[1]["tags"]
            assert "task_status:done" in change_post[1]["tags"]
            assert change_post[1]["source"] == "operational_tray"
            assert "stage_evidence=checkpoint:change-doc-1" in result
        finally:
            lease_store.close()

    async def test_project_work_executes_next_priority_as_open_tasks(self, monkeypatch):
        requested: list[str] = []

        async def fake_get(api_base: str, path: str):
            requested.append(path)
            return {
                "items": [
                    {
                        "artifact_key": "task:alpha:task-1",
                        "title": "First task",
                        "status": "open",
                    }
                ]
            }

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        result = await mcp_sse._execute_tool(
            "project_work",
            {"project": "alpha", "intent": "what is the next priority?", "limit": 5},
            "http://test",
        )

        data = json.loads(result)
        assert data["status"] == "executed"
        assert data["selected_route"]["tool"] == "list_open_tasks"
        assert data["selected_route"]["intent_type"] == "next_priority"
        assert data["executed"] is True
        assert data["action_status"] == "executed"
        assert data["agent_action"]["action_status"] == "executed"
        assert data["agent_action"]["recommended_next_call"] is None
        assert data["route_telemetry"]["facade"] == "project_work"
        assert data["route_telemetry"]["underlying_tool"] == "list_open_tasks"
        assert data["route_telemetry"]["executed"] is True
        assert data["route_telemetry"]["guardrail_triggered"] is False
        assert data["compact_result"] == [
            {
                "artifact_key": "task:alpha:task-1",
                "type": "task",
                "title": "First task",
                "status": "open",
            }
        ]
        assert requested[0].startswith("/artifacts?project=alpha&status=open&limit=5")
        assert data["result"]["items"][0]["artifact_key"] == "task:alpha:task-1"

    async def test_project_work_list_active_tasks_keeps_improvement_embryos_actionable(self, monkeypatch):
        requested: list[str] = []

        async def fake_get(api_base: str, path: str):
            requested.append(path)
            return {
                "items": [
                    {
                        "artifact_key": "improvement:alpha:imp-1",
                        "type": "improvement",
                        "title": "High priority backend issue",
                        "status": "open",
                        "linked_artifact_key": "task:alpha:task-linked",
                        "linked_status": "archived",
                    }
                ]
            }

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        result = await mcp_sse._execute_tool(
            "project_work",
            {"project": "alpha", "intent": "list active tasks", "limit": 5},
            "http://test",
        )

        data = json.loads(result)
        assert data["selected_route"]["tool"] == "list_open_tasks"
        assert requested[0] == "/artifacts?project=alpha&status=open&limit=5"
        item = data["compact_result"][0]
        assert item["type"] == "improvement"
        assert item["linked_task_id"] == "task-linked"
        assert "task_id" not in item
        assert item["next_detail_form"]["payload"]["task_id"] == "task-linked"
        assert item["next_action"] == "Review the improvement or open its linked task before claiming work."

    async def test_project_work_list_open_work_keeps_work_items_broad(self, monkeypatch):
        requested: list[str] = []

        async def fake_get(api_base: str, path: str):
            requested.append(path)
            return {"items": []}

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        await mcp_sse._execute_tool(
            "project_work",
            {"project": "alpha", "intent": "list open work items", "limit": 5},
            "http://test",
        )

        assert requested[0] == "/artifacts?project=alpha&status=open&limit=5"

    async def test_project_work_next_priority_skips_claimed_tasks(self, monkeypatch):
        from pathlib import Path
        from app.services import task_lease_service as lease_mod

        store = lease_mod.TaskLeaseStore(Path(":memory:"))
        monkeypatch.setattr(lease_mod, "_STORE", store)

        async def fake_get(api_base: str, path: str):
            return {
                "items": [
                    {
                        "artifact_key": "task:alpha:task-1",
                        "task_id": "task-1",
                        "title": "Busy task",
                        "status": "open",
                    },
                    {
                        "artifact_key": "task:alpha:task-2",
                        "task_id": "task-2",
                        "title": "Free task",
                        "status": "open",
                    },
                ]
            }

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        try:
            store.claim(project="alpha", task_id="task-1", owner_agent="claude", session_id="sess-claude")
            result = await mcp_sse._execute_tool(
                "project_work",
                {"project": "alpha", "intent": "what is the next priority?", "limit": 5},
                "http://test",
            )
            data = json.loads(result)

            assert data["result"]["claim_filter"] == "available"
            assert data["result"]["claim_summary"]["hidden_claimed"] == 1
            assert [item["title"] for item in data["result"]["items"]] == ["Free task"]
            assert data["compact_result"][0]["title"] == "Free task"
        finally:
            store.close()

    async def test_project_work_can_request_claimed_tasks(self, monkeypatch):
        from pathlib import Path
        from app.services import task_lease_service as lease_mod

        store = lease_mod.TaskLeaseStore(Path(":memory:"))
        monkeypatch.setattr(lease_mod, "_STORE", store)

        async def fake_get(api_base: str, path: str):
            return {
                "items": [
                    {
                        "artifact_key": "task:alpha:task-1",
                        "task_id": "task-1",
                        "title": "Busy task",
                        "status": "open",
                    },
                    {
                        "artifact_key": "task:alpha:task-2",
                        "task_id": "task-2",
                        "title": "Free task",
                        "status": "open",
                    },
                ]
            }

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        try:
            store.claim(project="alpha", task_id="task-1", owner_agent="claude", session_id="sess-claude")
            result = await mcp_sse._execute_tool(
                "project_work",
                {"project": "alpha", "intent": "show occupied open tasks", "limit": 5},
                "http://test",
            )
            data = json.loads(result)

            assert data["result"]["claim_filter"] == "claimed"
            assert [item["title"] for item in data["result"]["items"]] == ["Busy task"]
            assert data["compact_result"][0]["claim_status"] == "claimed"
            assert data["compact_result"][0]["claimed_by"] == "claude"
        finally:
            store.close()

    async def test_project_work_multi_agent_assignment_requires_independent_tasks(self, monkeypatch):
        async def fake_get(api_base: str, path: str):
            return {
                "items": [
                    {
                        "artifact_key": "task:alpha:task-1",
                        "task_id": "task-1",
                        "title": "Needs dependency review",
                        "status": "open",
                    },
                    {
                        "artifact_key": "task:alpha:task-2",
                        "task_id": "task-2",
                        "title": "Independent docs task",
                        "status": "open",
                        "tags": ["parallel_safe"],
                    },
                ]
            }

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        result = await mcp_sse._execute_tool(
            "project_work",
            {"project": "alpha", "intent": "give another agent an independent parallel task", "limit": 5},
            "http://test",
        )
        data = json.loads(result)

        assert data["result"]["assignment_filter"] == "independent"
        assert data["result"]["assignment_policy"].startswith("Only tasks with explicit independence evidence")
        assert [item["title"] for item in data["result"]["items"]] == ["Independent docs task"]
        assert data["result"]["assignment_summary"]["needs_review"] == 1
        assert data["result"]["assignment_summary"]["hidden"] == 1

    async def test_project_work_next_priority_answer_is_final_answer_shaped(self, monkeypatch):
        async def fake_get(api_base: str, path: str):
            return {
                "items": [
                    {
                        "artifact_key": "task:alpha:task-1",
                        "task_id": "task-1",
                        "title": "First task",
                        "status": "open",
                    }
                ]
            }

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        text = await mcp_sse._execute_tool(
            "project_work",
            {"project": "alpha", "intent": "what is the next priority?", "response_format": "answer"},
            "http://test",
        )

        assert text.startswith("Mnemoforge answer\n")
        assert "Answer: Next useful project action is First task." in text
        assert "task_id=task-1" in text
        assert "title=First task" in text
        assert "task_status=open" in text
        assert "artifact_key=task:alpha:task-1" in text
        assert "why=" in text
        assert "route=list_open_tasks" not in text
        assert "intent_type=next_priority" not in text
        assert "scorer_backend=" not in text
        assert "selected_route" not in text

    async def test_project_work_catalog_matches_priority_paraphrase(self, monkeypatch):
        async def fake_get(api_base: str, path: str):
            return {"items": []}

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        result = await mcp_sse._execute_tool(
            "project_work",
            {"project": "alpha", "intent": "what should I do next?"},
            "http://test",
        )

        data = json.loads(result)
        assert data["selected_route"]["tool"] == "list_open_tasks"
        assert data["selected_route"]["matched_example"] == "what should i do next"
        assert data["selected_route"]["route_candidates"][0]["intent_type"] == "next_priority"

    async def test_project_work_executes_pull_task_context_route(self, monkeypatch):
        called: list[dict] = []

        async def fake_continue(api_base: str, args: dict):
            called.append(args)
            return {
                "project": args["project"],
                "task_id": args["task_id"],
                "status": "ready",
                "next_safe_action": "Continue implementation.",
            }

        monkeypatch.setattr(mcp_sse, "_build_pull_task_context_payload", fake_continue)
        result = await mcp_sse._execute_tool(
            "project_work",
            {"project": "alpha", "task_id": "task-1", "intent": "continue task"},
            "http://test",
        )

        data = json.loads(result)
        assert data["selected_route"]["tool"] == "pull_task_context"
        assert data["selected_route"]["intent_type"] == "pull_task_context"
        assert data["executed"] is True
        assert data["agent_action"]["one_sentence_summary"].startswith("project_work selected pull_task_context")
        assert data["agent_action"]["compact_result"]["next_safe_action"] == "Continue implementation."
        assert called[0]["task_id"] == "task-1"
        assert called[0]["include_handoffs"] is True

    async def test_project_work_routes_localized_take_task_via_learned_pattern(self, monkeypatch):
        class FakeRoutePatternStore:
            def match(self, **kwargs):
                return {
                    "pattern_id": "project-work-local-start",
                    "intent_type": "start_task_session",
                    "tool": "start_task_session",
                    "confidence": 0.91,
                    "matched_example": "localized start task intent",
                    "reason": "Matched a learned localized project_work route.",
                    "backend_used": "learned_semantic",
                    "score": 0.82,
                    "matched_by": "semantic",
                }

        monkeypatch.setattr(mcp_sse, "get_route_pattern_store", lambda: FakeRoutePatternStore())
        result = await mcp_sse._execute_tool(
            "project_work",
            {
                "project": "alpha",
                "task_id": "task-1",
                "session_id": "sess-codex",
                "intent": "profile phrase alpha one",
            },
            "http://test",
        )
        data = json.loads(result)
        assert data["status"] == "planned"
        assert data["selected_route"]["tool"] == "start_task_session"
        assert data["selected_route"]["intent_type"] == "start_task_session"
        assert data["selected_route"]["scorer"]["backend_used"] == "learned_semantic"
        assert data["executed"] is False

    async def test_project_work_routes_localized_finish_task_via_learned_pattern(self, monkeypatch):
        class FakeRoutePatternStore:
            def match(self, **kwargs):
                return {
                    "pattern_id": "project-work-local-finish",
                    "intent_type": "finish_task_session",
                    "tool": "finish_task_session",
                    "confidence": 0.91,
                    "matched_example": "localized finish task intent",
                    "reason": "Matched a learned localized project_work route.",
                    "backend_used": "learned_semantic",
                    "score": 0.82,
                    "matched_by": "semantic",
                }

        monkeypatch.setattr(mcp_sse, "get_route_pattern_store", lambda: FakeRoutePatternStore())
        result = await mcp_sse._execute_tool(
            "project_work",
            {
                "project": "alpha",
                "task_id": "task-1",
                "work_id": "work-1",
                "session_id": "sess-codex",
                "intent": "profile phrase beta two",
            },
            "http://test",
        )
        data = json.loads(result)
        assert data["status"] == "planned"
        assert data["selected_route"]["tool"] == "finish_task_session"
        assert data["selected_route"]["intent_type"] == "finish_task_session"
        assert data["selected_route"]["scorer"]["backend_used"] == "learned_semantic"
        assert data["executed"] is False

    async def test_project_work_plans_close_tail_without_mutation_by_default(self, monkeypatch):
        result = await mcp_sse._execute_tool(
            "project_work",
            {
                "project": "alpha",
                "task_id": "task-1",
                "intent": "close the tail",
                "summary": "Closed handoff evidence gap.",
                "changed_files": ["app/routers/mcp_sse.py"],
            },
            "http://test",
        )

        data = json.loads(result)
        assert data["status"] == "planned"
        assert data["selected_route"]["tool"] == "record_work_result"
        assert data["selected_route"]["mutating"] is True
        assert data["executed"] is False
        assert data["action_status"] == "needs_confirmation"
        assert data["agent_action"]["confirmation_required"] is True
        assert data["agent_action"]["recommended_next_call"]["tool"] == "project_work"
        assert data["agent_action"]["recommended_next_call"]["arguments"]["allow_mutation"] is True
        assert data["agent_action"]["do_not_call"] == ["record_task_checkpoint", "record_work_result"]
        assert data["submit_payload"]["summary"] == "Closed handoff evidence gap."
        assert "allow_mutation=true" in data["warnings"][0]

    async def test_project_work_allow_mutation_still_requires_owned_claim(self, monkeypatch):
        from pathlib import Path
        from app.services import task_lease_service as lease_mod

        lease_store = lease_mod.TaskLeaseStore(Path(":memory:"))
        monkeypatch.setattr(lease_mod, "_STORE", lease_store)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        async def fail_post(api_base: str, path: str, payload: dict):
            raise AssertionError(f"unexpected POST path: {path}")

        monkeypatch.setattr(mcp_sse, "_post", fail_post)
        try:
            result = await mcp_sse._execute_tool(
                "project_work",
                {
                    "project": "alpha",
                    "task_id": "task-1",
                    "intent": "close the tail",
                    "summary": "Closed handoff evidence gap.",
                    "allow_mutation": True,
                    "agent_id": "codex",
                    "session_id": "sess-1",
                },
                "http://test",
            )
            data = json.loads(result)
            assert data["status"] == "executed"
            assert data["result"]["status"] == "conflict"
            assert data["result"]["error"] == "active_claim_required"
        finally:
            lease_store.close()

    async def test_project_work_allow_mutation_executes_finish_with_owned_claim(self, monkeypatch):
        from pathlib import Path
        from app.services import task_lease_service as lease_mod

        lease_store = lease_mod.TaskLeaseStore(Path(":memory:"))
        monkeypatch.setattr(lease_mod, "_STORE", lease_store)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())
        calls: list[tuple[str, dict]] = []

        async def fake_execute_tool(name: str, args: dict, api_base: str, session_id: str | None = None):
            calls.append((name, args))
            assert name == "finish_task_session"
            assert args["project"] == "alpha"
            assert args["task_id"] == "task-1"
            assert args["work_id"] == "work-1"
            assert args["session_id"] == "sess-1"
            return json.dumps({"status": "finished", "task_id": "task-1"})

        monkeypatch.setattr(mcp_sse, "_execute_tool", fake_execute_tool)

        try:
            claim_result = lease_store.claim(project="alpha", task_id="task-1", owner_agent="codex", session_id="sess-1")
            work_token = claim_result.work_token
            data = await mcp_sse._build_project_work_payload(
                "http://test",
                {
                    "project": "alpha",
                    "task_id": "task-1",
                    "work_id": "work-1",
                    "agent_id": "codex",
                    "session_id": "sess-1",
                    "intent": "finish task session",
                    "summary": "Completed lifecycle work.",
                    "verification": ["verification_contour:alpha:focused-tests passed"],
                    "changed_files": ["app/routers/mcp_sse.py"],
                    "next_step": "none",
                    "allow_mutation": True,
                    "work_token": work_token,
                },
            )
            assert data["status"] == "executed"
            assert data["action_status"] == "executed"
            assert data["selected_route"]["tool"] == "finish_task_session"
            assert data["result"]["status"] == "finished", data["result"]
            assert calls
        finally:
            lease_store.close()

    async def test_project_work_catalog_matches_closeout_paraphrase(self):
        result = await mcp_sse._execute_tool(
            "project_work",
            {
                "project": "alpha",
                "task_id": "task-1",
                "intent": "finish the remaining lifecycle gap",
                "summary": "Lifecycle evidence is now complete.",
            },
            "http://test",
        )

        data = json.loads(result)
        assert data["selected_route"]["tool"] == "record_work_result"
        assert data["selected_route"]["intent_type"] == "capture_or_closeout"
        assert data["action_status"] == "needs_confirmation"
        assert data["selected_route"]["scorer"]["backend_used"] == "lexical"

    async def test_project_work_routes_explicit_new_task_creation_without_continue_fallback(self, monkeypatch):
        async def forbidden_disambiguate(text: str, args: dict, candidates: list[dict]):
            raise AssertionError("explicit task creation should stay deterministic")

        monkeypatch.setattr(mcp_sse, "_project_work_llm_disambiguate", forbidden_disambiguate)
        result = await mcp_sse._execute_tool(
            "project_work",
            {
                "project": "alpha",
                "intent": "create improvement task for multi-agent task leasing and timeout release",
                "summary": "Profile note gamma three.",
                "scorer_backend": "auto",
            },
            "http://test",
        )

        data = json.loads(result)
        assert data["status"] == "planned"
        assert data["selected_route"]["tool"] == "record_work_result"
        assert data["selected_route"]["intent_type"] == "create_task"
        assert data["selected_route"]["route_candidates"][0]["intent_type"] == "create_task"
        assert data["selected_route"]["scorer"]["backend_used"] == "lexical"
        assert data["submit_payload"]["create_issue_if_unmatched"] is True
        assert data["submit_payload"]["skip_auto_task_match"] is True
        assert data["submit_payload"]["task_id"] == ""
        assert data["action_status"] == "needs_confirmation"
        assert data["weak_model_guardrail"]["mutation_executed"] is False
        assert data["weak_model_guardrail"]["confirmation_required"] is True
        assert data["weak_model_guardrail"]["do_not_claim_created"] is True
        assert "No task" in data["weak_model_guardrail"]["plain_instruction"]

    async def test_project_work_allow_mutation_executes_create_task_without_existing_task_claim(self, monkeypatch):
        calls: list[tuple[str, dict]] = []

        async def fake_execute_tool(name: str, args: dict, api_base: str, session_id: str | None = None):
            calls.append((name, args))
            assert name == "record_work_result"
            assert args["task_id"] == ""
            assert args["create_issue_if_unmatched"] is True
            assert args["skip_auto_task_match"] is True
            return json.dumps({
                "status": "recorded",
                "route": ["memory", "improvement"],
                "created_issue": {"id": "issue-1", "status": "open"},
            })

        monkeypatch.setattr(mcp_sse, "_execute_tool", fake_execute_tool)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        data = await mcp_sse._build_project_work_payload(
            "http://test",
            {
                "project": "alpha",
                "intent": "create improvement task for memory attribution",
                "summary": "Create a governed backlog item for memory attribution.",
                "allow_mutation": True,
                "scorer_backend": "lexical",
            },
        )

        assert data["status"] == "executed"
        assert data["action_status"] == "executed"
        assert data["executed"] is True
        assert data["result"]["created_issue"]["id"] == "issue-1"
        assert calls

    async def test_project_work_routes_localized_new_task_creation_via_learned_pattern(self, monkeypatch):
        class FakeRoutePatternStore:
            def match(self, **kwargs):
                return {
                    "pattern_id": "project-work-local-create",
                    "intent_type": "create_task",
                    "tool": "record_work_result",
                    "confidence": 0.91,
                    "matched_example": "localized create task intent",
                    "reason": "Matched a learned localized project_work route.",
                    "backend_used": "learned_semantic",
                    "score": 0.82,
                    "matched_by": "semantic",
                }

        monkeypatch.setattr(mcp_sse, "get_route_pattern_store", lambda: FakeRoutePatternStore())
        result = await mcp_sse._execute_tool(
            "project_work",
            {
                "project": "alpha",
                "intent": "profile phrase gamma three",
                "summary": "Profile note gamma three.",
            },
            "http://test",
        )

        data = json.loads(result)
        assert data["selected_route"]["intent_type"] == "create_task"
        assert data["selected_route"]["tool"] == "record_work_result"
        assert data["selected_route"]["scorer"]["backend_used"] == "learned_semantic"
        assert data["submit_payload"]["create_issue_if_unmatched"] is True
        assert data["submit_payload"]["skip_auto_task_match"] is True

    async def test_project_work_guarded_create_task_answer_is_hard_to_misread(self):
        text = await mcp_sse._execute_tool(
            "project_work",
            {
                "project": "alpha",
                "intent": "create task for mcp functional overview",
                "response_format": "answer",
            },
            "http://test",
        )

        assert text.startswith("Mnemoforge answer\n")
        assert "Answer: No mutation was executed." in text
        assert "executed=false" in text
        assert "mutation_executed=false" in text
        assert "confirmation_required=true" in text
        assert "do_not_claim_created=true" in text

    async def test_project_work_guarded_create_task_diagnostic_has_execution_state(self):
        text = await mcp_sse._execute_tool(
            "project_work",
            {
                "project": "alpha",
                "intent": "create task for mcp functional overview",
                "response_format": "diagnostic",
            },
            "http://test",
        )

        assert text.startswith("Mnemoforge route diagnostic\n")
        assert "executed=false" in text
        assert "guardrail_triggered=true" in text
        assert "confirmation_required=true" in text

    async def test_project_work_checkpoint_about_create_task_route_is_not_new_task_creation(self):
        result = await mcp_sse._execute_tool(
            "project_work",
            {
                "project": "alpha",
                "task_id": "facade-create-task-intent-routing",
                "intent": "record checkpoint for facade create-task intent routing fix",
                "summary": "Implemented routing fix.",
            },
            "http://test",
        )

        data = json.loads(result)
        assert data["selected_route"]["intent_type"] == "capture_or_closeout"
        assert data["selected_route"]["tool"] == "record_work_result"
        assert data["submit_payload"].get("create_issue_if_unmatched") is not True
        assert data["submit_payload"].get("skip_auto_task_match") is not True

    async def test_project_work_auto_keeps_strong_closeout_signal_deterministic(self, monkeypatch):
        async def forbidden_disambiguate(text: str, args: dict, candidates: list[dict]):
            raise AssertionError("strong route catalog match should not call LLM in auto mode")

        monkeypatch.setattr(mcp_sse, "_project_work_llm_disambiguate", forbidden_disambiguate)
        result = await mcp_sse._execute_tool(
            "project_work",
            {
                "project": "alpha",
                "task_id": "task-1",
                "intent": "finish up",
                "summary": "Ready to wrap this work.",
                "scorer_backend": "auto",
            },
            "http://test",
        )

        data = json.loads(result)
        assert data["selected_route"]["tool"] == "record_work_result"
        assert data["selected_route"]["matched_example"] == "finish up"
        assert data["selected_route"]["scorer"]["backend_used"] == "lexical"
        assert data["action_status"] == "needs_confirmation"

    async def test_project_work_llm_backend_can_disambiguate_low_confidence_intent(self, monkeypatch):
        async def fake_disambiguate(text: str, args: dict, candidates: list[dict]):
            assert "finish up" in text
            return {
                "intent_type": "capture_or_closeout",
                "confidence": 0.91,
                "matched_example": "wrap up the task",
                "reason": "The user wants to finish and save the current task work.",
            }

        monkeypatch.setattr(mcp_sse, "_project_work_llm_disambiguate", fake_disambiguate)
        result = await mcp_sse._execute_tool(
            "project_work",
            {
                "project": "alpha",
                "task_id": "task-1",
                "intent": "finish up",
                "summary": "Ready to wrap this work.",
                "scorer_backend": "llm",
            },
            "http://test",
        )

        data = json.loads(result)
        assert data["selected_route"]["tool"] == "record_work_result"
        assert data["selected_route"]["matched_example"] == "wrap up the task"
        assert data["selected_route"]["scorer"]["backend_used"] == "llm"
        assert data["selected_route"]["scorer"]["llm_attempted"] is True
        assert data["action_status"] == "needs_confirmation"

    async def test_project_work_auto_backend_falls_back_to_lexical_when_llm_fails(self, monkeypatch):
        async def broken_disambiguate(text: str, args: dict, candidates: list[dict]):
            raise RuntimeError("classifier unavailable")

        monkeypatch.setattr(mcp_sse, "_project_work_llm_disambiguate", broken_disambiguate)
        result = await mcp_sse._execute_tool(
            "project_work",
            {"project": "alpha", "intent": "unclear project thing", "scorer_backend": "auto"},
            "http://test",
        )

        data = json.loads(result)
        assert data["selected_route"]["tool"] == "tool_recommend"
        assert data["selected_route"]["scorer"]["backend_used"] == "lexical"
        assert data["selected_route"]["scorer"]["llm_attempted"] is True
        assert "classifier unavailable" in data["selected_route"]["scorer"]["fallback_reason"]

    async def test_project_work_routes_restart_validation_to_execution_context(self, monkeypatch):
        posted: list[tuple[str, dict]] = []

        async def fake_post(api_base: str, path: str, payload: dict):
            posted.append((path, payload))
            return {
                "project": payload["project"],
                "state": payload["state"],
                "risk_controls": ["wait stale window"],
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        result = await mcp_sse._execute_tool(
            "project_work",
            {
                "project": "alpha",
                "task_id": "task-1",
                "intent": "restart and validate the server",
                "changed_files": ["app/main.py"],
            },
            "http://test",
        )

        data = json.loads(result)
        assert data["selected_route"]["tool"] == "get_task_execution_context"
        assert data["selected_route"]["intent_type"] == "verify_or_live_validate"
        assert posted[0][0] == "/task-execution-context"
        assert posted[0][1]["state"] == "live_validation"
        assert posted[0][1]["changed_files"] == ["app/main.py"]
        assert data["agent_action"]["compact_result"]["risk_controls"] == ["wait stale window"]

    async def test_project_work_reviews_task_capture_candidates(self, monkeypatch):
        fetched: list[str] = []

        async def fake_get(api_base: str, path: str):
            fetched.append(path)
            return {
                "found": 1,
                "candidates": [
                    {
                        "artifact_id": "candidate-1",
                        "kind": "definition_of_done",
                        "content": "The facade routes capture review without manual tool hunting.",
                        "confidence": 0.8,
                    }
                ],
            }

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        result = await mcp_sse._execute_tool(
            "project_work",
            {
                "project": "alpha",
                "task_id": "task-1",
                "intent": "review pending capture drafts",
            },
            "http://test",
        )

        data = json.loads(result)
        assert data["status"] == "executed"
        assert data["selected_route"]["tool"] == "task_capture_review"
        assert data["selected_route"]["intent_type"] == "review_task_capture"
        assert fetched == ["/project/tasks/task-1/capture-candidates?project=alpha&limit=10"]
        assert data["compact_result"]["found"] == 1
        assert data["compact_result"]["candidates"][0]["kind"] == "definition_of_done"

    async def test_project_work_capture_review_requires_task_id(self):
        result = await mcp_sse._execute_tool(
            "project_work",
            {"project": "alpha", "intent": "show capture candidates"},
            "http://test",
        )

        data = json.loads(result)
        assert data["status"] == "planned"
        assert data["selected_route"]["tool"] == "task_capture_review"
        assert data["action_status"] == "ready"
        assert "requires task_id" in data["warnings"][0]

    async def test_project_work_routes_checkpoint_draft_rejection_directly(self):
        result = await mcp_sse._execute_tool(
            "project_work",
            {
                "project": "alpha",
                "intent": "reject checkpoint draft",
                "draft_id": "draft-1",
                "version": 2,
                "reason": "Needs clearer verification.",
            },
            "http://test",
        )

        data = json.loads(result)
        assert data["status"] == "planned"
        assert data["selected_route"]["tool"] == "reject_checkpoint_draft"
        assert data["selected_route"]["intent_type"] == "reject_checkpoint_draft"
        assert data["submit_payload"]["draft_id"] == "draft-1"
        assert data["submit_payload"]["version"] == 2
        assert data["submit_payload"]["reason"] == "Needs clearer verification."

    async def test_project_work_routes_checkpoint_draft_approval_directly(self):
        result = await mcp_sse._execute_tool(
            "project_work",
            {
                "project": "alpha",
                "intent": "approve checkpoint draft",
                "draft_id": "draft-1",
                "version": 1,
            },
            "http://test",
        )

        data = json.loads(result)
        assert data["status"] == "planned"
        assert data["selected_route"]["tool"] == "approve_checkpoint_draft"
        assert data["selected_route"]["intent_type"] == "approve_checkpoint_draft"
        assert data["submit_payload"]["draft_id"] == "draft-1"

    async def test_project_work_routes_rule_capture_to_guarded_project_rules_plan(self):
        result = await mcp_sse._execute_tool(
            "project_work",
            {"project": "alpha", "intent": "make this a project policy"},
            "http://test",
        )

        data = json.loads(result)
        assert data["status"] == "planned"
        assert data["selected_route"]["tool"] == "project_rules"
        assert data["selected_route"]["intent_type"] == "rule_work"
        assert data["executed"] is False
        assert data["action_status"] == "needs_confirmation"
        assert data["agent_action"]["do_not_call"] == ["promote_rule_candidate", "revise_law_from_rule_candidate"]
        assert "suggested_first_tools" in data["submit_payload"]

    async def test_project_rules_executes_read_only_law_listing(self, monkeypatch):
        seen: list[tuple[str, str]] = []

        async def fake_get(api_base: str, path: str):
            seen.append((api_base, path))
            return {
                "items": [
                    {
                        "id": "law-1",
                        "title": "Use Docker test contour",
                        "status": "active",
                        "scope": "project",
                        "project": "alpha",
                        "is_project_local": True,
                    }
                ]
            }

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        result = await mcp_sse._execute_tool(
            "project_rules",
            {"project": "alpha", "intent": "check active project laws"},
            "http://test",
        )

        data = json.loads(result)
        assert data["status"] == "executed"
        assert data["selected_route"]["tool"] == "list_project_laws"
        assert data["selected_route"]["intent_type"] == "list_laws"
        assert data["executed"] is True
        assert seen[0][1] == "/laws?status=active&limit=100&include_promoted=true&project=alpha"
        assert "Use Docker test contour" in data["result"]

    async def test_project_rules_routes_docker_rule_lookup_to_list_laws(self, monkeypatch):
        seen: list[str] = []

        async def fake_get(api_base: str, path: str):
            seen.append(path)
            return {"items": []}

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        result = await mcp_sse._execute_tool(
            "project_rules",
            {"project": "supermemory", "intent": "find active docker-related rules and return ids"},
            "http://test",
        )

        data = json.loads(result)
        assert data["status"] == "executed"
        assert data["selected_route"]["tool"] == "list_project_laws"
        assert data["selected_route"]["intent_type"] == "list_laws"
        assert seen[0] == "/laws?status=active&limit=100&include_promoted=true&project=supermemory"

    async def test_project_rules_includes_project_context_testing_rules_when_laws_empty(self, monkeypatch):
        seen: list[str] = []

        async def fake_get(api_base: str, path: str):
            seen.append(path)
            return {"items": []}

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        monkeypatch.setattr(
            mcp_sse,
            "_project_context_rule_refs",
            lambda args: [
                {
                    "id": "docker-rule-1",
                    "title": "MnemoForge Docker test contour",
                    "status": "active",
                    "scope": "project",
                    "project": args["project"],
                    "topic_path": "mnemoforge/testing/docker-test-contour",
                    "rationale": "Use the declared project verification contour; do not run host pytest.",
                    "is_project_local": True,
                    "source": "project_context",
                }
            ],
        )

        result = await mcp_sse._execute_tool(
            "project_rules",
            {
                "project": "mnemoforge",
                "intent": "Which active rules mention Docker test contour and host pytest?",
            },
            "http://test",
        )

        data = json.loads(result)
        assert data["status"] == "executed"
        assert data["selected_route"]["tool"] == "list_project_laws"
        assert seen[0] == "/laws?status=active&limit=100&include_promoted=true&project=mnemoforge"
        assert "MnemoForge Docker test contour" in data["result"]
        assert "source=project_context" in data["result"]
        assert "No matching project laws" not in data["result"]

    async def test_project_rules_answer_is_compact_and_omits_route_metadata(self, monkeypatch):
        async def fake_get(api_base: str, path: str):
            return {
                "items": [
                    {
                        "id": "law-1",
                        "title": "Use Docker test contour",
                        "status": "active",
                        "scope": "project",
                        "project": "alpha",
                        "is_project_local": True,
                    }
                ]
            }

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        full = await mcp_sse._execute_tool(
            "project_rules",
            {"project": "alpha", "intent": "check active project laws"},
            "http://test",
        )
        answer = await mcp_sse._execute_tool(
            "project_rules",
            {"project": "alpha", "intent": "check active project laws", "response_format": "answer"},
            "http://test",
        )

        assert answer.startswith("Mnemoforge answer\n")
        assert "project_rules executed route list_project_laws" in answer
        assert "Use Docker test contour" in answer
        assert "route_telemetry" not in answer
        assert "route_candidates" not in answer
        assert len(answer) < len(full) * 0.35

    async def test_project_rules_auto_uses_expert_and_learns_unknown_rule_query(self, monkeypatch):
        recorded: list[dict] = []
        seen: list[str] = []

        class FakeRoutePatternStore:
            def match(self, **kwargs):
                return None

            def record(self, **kwargs):
                recorded.append(kwargs)
                return "rule-pattern-1"

        async def fake_disambiguate(*, facade: str, text: str, args: dict, candidates: list[dict], catalog: tuple[dict, ...]):
            assert facade == "project_rules"
            return {
                "intent_type": "list_laws",
                "confidence": 0.93,
                "matched_example": "operator asks for rule identifiers",
                "reason": "The request asks to retrieve existing active rules.",
            }

        async def fake_get(api_base: str, path: str):
            seen.append(path)
            return {"items": []}

        monkeypatch.setattr(mcp_sse, "get_route_pattern_store", lambda: FakeRoutePatternStore())
        monkeypatch.setattr(mcp_sse, "_facade_llm_disambiguate", fake_disambiguate)
        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        result = await mcp_sse._execute_tool(
            "project_rules",
            {
                "project": "supermemory",
                "intent": "which operational constraint mentions containerized verification",
                "scorer_backend": "llm",
            },
            "http://test",
        )

        data = json.loads(result)
        assert data["selected_route"]["tool"] == "list_project_laws"
        assert data["selected_route"]["scorer"]["backend_used"] == "llm"
        assert data["route_telemetry"]["matched_pattern_id"] == "rule-pattern-1"
        assert recorded[0]["facade"] == "project_rules"
        assert recorded[0]["intent_type"] == "list_laws"
        assert seen[0] == "/laws?status=active&limit=100&include_promoted=true&project=supermemory"

    async def test_project_rules_auto_uses_learned_route_before_expert(self, monkeypatch):
        class FakeRoutePatternStore:
            def match(self, **kwargs):
                return {
                    "pattern_id": "rule-pattern-2",
                    "intent_type": "list_laws",
                    "tool": "list_project_laws",
                    "confidence": 0.91,
                    "matched_example": "learned rule lookup",
                    "reason": "Matched a learned project_rules route.",
                    "backend_used": "learned_semantic",
                    "score": 0.82,
                    "matched_by": "semantic",
                }

        async def forbidden_disambiguate(**kwargs):
            raise AssertionError("learned route should skip LLM")

        async def fake_get(api_base: str, path: str):
            return {"items": []}

        monkeypatch.setattr(mcp_sse, "get_route_pattern_store", lambda: FakeRoutePatternStore())
        monkeypatch.setattr(mcp_sse, "_facade_llm_disambiguate", forbidden_disambiguate)
        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        result = await mcp_sse._execute_tool(
            "project_rules",
            {"project": "supermemory", "intent": "operational constraint about containerized verification"},
            "http://test",
        )

        data = json.loads(result)
        assert data["selected_route"]["tool"] == "list_project_laws"
        assert data["selected_route"]["scorer"]["backend_used"] == "learned_semantic"
        assert data["route_telemetry"]["matched_pattern_id"] == "rule-pattern-2"

    async def test_project_rules_plans_candidate_promotion_without_mutation_by_default(self, monkeypatch):
        async def forbidden_execute(tool_name: str, args: dict, api_base: str, session_id=None):
            raise AssertionError("mutating route should not execute without allow_mutation")

        monkeypatch.setattr(mcp_sse, "_execute_tool", forbidden_execute)
        result = await mcp_sse._build_project_rules_payload(
            "http://test",
            {
                "project": "alpha",
                "intent": "promote this candidate to active law",
                "candidate_id": "cand-1",
                "reason": "User confirmed this project rule.",
                "target_status": "active",
            },
        )

        assert result["status"] == "planned"
        assert result["action_status"] == "needs_confirmation"
        assert result["selected_route"]["tool"] == "promote_rule_candidate"
        assert result["submit_payload"]["candidate_id"] == "cand-1"
        assert result["agent_action"]["recommended_next_call"]["arguments"]["allow_mutation"] is True
        assert result["agent_action"]["do_not_call"] == [
            "promote_rule_candidate",
            "revise_law_from_rule_candidate",
            "review_rule_candidate",
        ]

    async def test_project_rules_plans_new_law_proposal_without_memory_store(self, monkeypatch):
        async def forbidden_execute(tool_name: str, args: dict, api_base: str, session_id=None):
            raise AssertionError("mutating route should not execute without allow_mutation")

        monkeypatch.setattr(mcp_sse, "_execute_tool", forbidden_execute)
        result = await mcp_sse._build_project_rules_payload(
            "http://test",
            {
                "project": "ui_avt",
                "intent": "propose new law",
                "target_scope": "project",
                "title": "Architecture Separation: Frontend/Backend via MessageBus",
                "statement": "Frontend code MUST NOT import backend service modules directly.",
                "rationale": "Keep frontend and backend independently testable.",
            },
        )

        assert result["status"] == "planned"
        assert result["action_status"] == "needs_confirmation"
        assert result["selected_route"]["tool"] == "create_rule_candidate"
        assert result["submit_payload"]["project"] == "ui_avt"
        assert result["submit_payload"]["target_status"] == "trial"
        assert result["agent_action"]["recommended_next_call"]["tool"] == "project_rules"
        assert result["agent_action"]["recommended_next_call"]["arguments"]["allow_mutation"] is True
        assert "memory_store" in result["agent_action"]["do_not_call"]
        assert "memory_store" in result["next_safe_action"]

    async def test_project_rules_structural_law_proposal_beats_stale_learned_route(self, monkeypatch):
        disabled: list[tuple[str, str]] = []

        class StaleRoutePatternStore:
            def match(self, **kwargs):
                return {
                    "intent_type": "list_candidates",
                    "tool": "list_rule_candidates",
                    "confidence": 0.99,
                    "pattern_id": "stale-rule-route",
                    "score": 0.99,
                    "matched_by": "semantic",
                }

            def disable_pattern(self, pattern_id: str, *, reason: str, metadata: dict | None = None):
                disabled.append((pattern_id, reason))
                return True

        async def forbidden_execute(tool_name: str, args: dict, api_base: str, session_id=None):
            raise AssertionError("structural mutating route should not execute without allow_mutation")

        monkeypatch.setattr(mcp_sse, "get_route_pattern_store", lambda: StaleRoutePatternStore())
        monkeypatch.setattr(mcp_sse, "_execute_tool", forbidden_execute)
        result = await mcp_sse._build_project_rules_payload(
            "http://test",
            {
                "project": "ui_avt",
                "intent": "propose new law",
                "title": "Architecture Separation: Frontend/Backend via MessageBus",
                "statement": "Frontend code MUST NOT import backend service modules directly.",
            },
        )

        assert result["status"] == "planned"
        assert result["selected_route"]["tool"] == "create_rule_candidate"
        assert result["selected_route"]["intent_type"] == "propose_law"
        assert result["selected_route"]["scorer"]["backend_used"] == "lexical"
        assert result["route_telemetry"]["matched_pattern_id"] == ""
        assert disabled == [("stale-rule-route", "conflicts_with_structural_route")]
        assert result["selected_route"]["scorer"]["invalidated_learned_pattern_id"] == "stale-rule-route"

    async def test_project_rules_executes_new_law_proposal_when_confirmed(self, monkeypatch):
        posted: list[tuple[str, dict]] = []

        async def fake_post(api_base: str, path: str, payload: dict):
            posted.append((path, payload))
            return {
                "candidate_id": "candidate-1",
                "project": payload["project"],
                "scope": payload["scope"],
                "status": payload["status"],
                "statement": payload["statement"],
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        result = await mcp_sse._execute_tool(
            "project_rules",
            {
                "project": "ui_avt",
                "intent": "propose new law",
                "target_scope": "project",
                "title": "Architecture Separation: Frontend/Backend via MessageBus",
                "statement": "Frontend code MUST NOT import backend service modules directly.",
                "rationale": "Keep frontend and backend independently testable.",
                "allow_mutation": True,
            },
            "http://test",
        )

        data = json.loads(result)
        assert data["status"] == "executed"
        assert data["selected_route"]["tool"] == "create_rule_candidate"
        assert posted == [
            (
                "/laws/candidates",
                {
                    "project": "ui_avt",
                    "title": "Architecture Separation: Frontend/Backend via MessageBus",
                    "statement": "Frontend code MUST NOT import backend service modules directly.",
                    "rationale": "Keep frontend and backend independently testable.",
                    "evidence_refs": [],
                    "scope": "project",
                    "status": "trial",
                    "confidence": 0.75,
                    "promotion_hint": "Review this trial rule after practical use.",
                    "review_after_days": 7,
                    "trial_days": 30,
                    "acted_by": "codex",
                    "source": "mcp_project_rules",
                },
            )
        ]
        assert data["result"]["candidate_id"] == "candidate-1"

    async def test_project_rules_executes_review_packet_for_forgetfulness_intent(self, monkeypatch):
        async def fake_post(api_base: str, path: str, payload: dict):
            assert path == "/laws/candidates/review-packet"
            return {"project": payload["project"], "groups": [], "status": payload["status"]}

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        result = await mcp_sse._execute_tool(
            "project_rules",
            {"project": "alpha", "intent": "why did you forget this rule?", "limit": 5},
            "http://test",
        )

        data = json.loads(result)
        assert data["status"] == "executed"
        assert data["selected_route"]["tool"] == "get_rule_candidate_review_packet"
        assert data["selected_route"]["intent_type"] == "review_candidates"
        assert data["result"]["project"] == "alpha"

    async def test_project_rules_infers_due_trial_review_from_intent(self, monkeypatch):
        posted: list[dict] = []

        async def fake_post(api_base: str, path: str, payload: dict):
            assert path == "/laws/candidates/review-packet"
            posted.append(payload)
            return {"project": payload["project"], "groups": [], "status": payload["status"]}

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        result = await mcp_sse._execute_tool(
            "project_rules",
            {"project": "alpha", "intent": "show due trial rules", "limit": 5},
            "http://test",
        )

        data = json.loads(result)
        assert data["status"] == "executed"
        assert data["selected_route"]["tool"] == "get_rule_candidate_review_packet"
        assert data["submit_payload"]["status"] == "trial"
        assert data["submit_payload"]["review_due"] is True
        assert posted == [
            {
                "project": "alpha",
                "status": "trial",
                "limit": 5,
                "max_matches": 5,
                "review_due": True,
            }
        ]

    async def test_project_context_routes_constraints_to_project_rules(self, monkeypatch):
        called: list[tuple[str, dict]] = []

        async def fake_execute(tool_name: str, args: dict, api_base: str, session_id=None):
            called.append((tool_name, args))
            return json.dumps({"status": "executed", "selected_route": {"tool": "list_project_laws"}})

        monkeypatch.setattr(mcp_sse, "_execute_tool", fake_execute)
        result = await mcp_sse._build_project_context_payload(
            "http://test",
            {"project": "alpha", "intent": "what project constraints matter here?"},
        )

        assert result["status"] == "executed"
        assert result["selected_route"]["tool"] == "project_rules"
        assert result["selected_route"]["intent_type"] == "rules_context"
        assert result["selected_route"]["scorer"]["backend_used"] == "lexical"
        assert result["route_telemetry"]["scorer_backend"] == "lexical"
        assert called[0][0] == "project_rules"
        assert called[0][1]["project"] == "alpha"

    async def test_project_context_llm_backend_can_disambiguate_to_reconstruction(self, monkeypatch):
        called: list[tuple[str, dict]] = []

        async def fake_disambiguate(*, facade: str, text: str, args: dict, candidates: list[dict], catalog: tuple[dict, ...]):
            assert facade == "project_context"
            return {
                "intent_type": "reconstruction_bundle",
                "confidence": 0.93,
                "matched_example": "reconstruct project from memory",
                "reason": "The user asks for recovery context.",
            }

        async def fake_execute(tool_name: str, args: dict, api_base: str, session_id=None):
            called.append((tool_name, args))
            return "Reconstruction bundle"

        monkeypatch.setattr(mcp_sse, "_facade_llm_disambiguate", fake_disambiguate)
        monkeypatch.setattr(mcp_sse, "_execute_tool", fake_execute)
        result = await mcp_sse._build_project_context_payload(
            "http://test",
            {"project": "alpha", "intent": "make a recovery packet for a fresh agent", "scorer_backend": "llm"},
        )

        assert result["selected_route"]["tool"] == "get_project_reconstruction_bundle"
        assert result["selected_route"]["scorer"]["backend_used"] == "llm"
        assert result["selected_route"]["scorer"]["llm_attempted"] is True
        assert result["route_telemetry"]["scorer_backend"] == "llm"
        assert called[0][0] == "get_project_reconstruction_bundle"

    async def test_project_context_default_auto_uses_llm_for_ambiguous_route_and_learns_pattern(self, monkeypatch):
        recorded: list[dict] = []

        class FakeRoutePatternStore:
            def match(self, **kwargs):
                return None

            def record(self, **kwargs):
                recorded.append(kwargs)
                return "pattern-1"

        async def fake_disambiguate(*, facade: str, text: str, args: dict, candidates: list[dict], catalog: tuple[dict, ...]):
            assert facade == "project_context"
            return {
                "intent_type": "project_readiness",
                "confidence": 0.91,
                "matched_example": "check project readiness",
                "reason": "The request asks whether the project is ready.",
            }

        async def fake_execute(tool_name: str, args: dict, api_base: str, session_id=None):
            return json.dumps({"project_id": args["project_id"], "ready": True})

        monkeypatch.setattr(mcp_sse, "get_route_pattern_store", lambda: FakeRoutePatternStore())
        monkeypatch.setattr(mcp_sse, "_facade_llm_disambiguate", fake_disambiguate)
        monkeypatch.setattr(mcp_sse, "_execute_tool", fake_execute)
        result = await mcp_sse._build_project_context_payload(
            "http://test",
            {"project": "alpha", "intent": "can this repo be used yet"},
        )

        assert result["selected_route"]["tool"] == "get_project_readiness"
        assert result["selected_route"]["scorer"]["backend_requested"] == "auto"
        assert result["selected_route"]["scorer"]["backend_used"] == "llm"
        assert result["route_telemetry"]["matched_pattern_id"] == "pattern-1"
        assert recorded[0]["facade"] == "project_context"
        assert recorded[0]["intent_type"] == "project_readiness"

    async def test_project_context_default_auto_uses_learned_route_before_llm(self, monkeypatch):
        class FakeRoutePatternStore:
            def match(self, **kwargs):
                return {
                    "pattern_id": "pattern-2",
                    "intent_type": "reconstruction_bundle",
                    "tool": "get_project_reconstruction_bundle",
                    "confidence": 0.89,
                    "matched_example": "reconstruct project from memory",
                    "reason": "Matched a learned route pattern.",
                    "backend_used": "learned_semantic",
                    "score": 0.82,
                    "matched_by": "semantic",
                }

        async def forbidden_disambiguate(**kwargs):
            raise AssertionError("learned route should skip LLM")

        async def fake_execute(tool_name: str, args: dict, api_base: str, session_id=None):
            return "Reconstruction bundle"

        monkeypatch.setattr(mcp_sse, "get_route_pattern_store", lambda: FakeRoutePatternStore())
        monkeypatch.setattr(mcp_sse, "_facade_llm_disambiguate", forbidden_disambiguate)
        monkeypatch.setattr(mcp_sse, "_execute_tool", fake_execute)
        result = await mcp_sse._build_project_context_payload(
            "http://test",
            {"project": "alpha", "intent": "fresh agent recovery packet"},
        )

        assert result["selected_route"]["tool"] == "get_project_reconstruction_bundle"
        assert result["selected_route"]["scorer"]["backend_used"] == "learned_semantic"
        assert result["selected_route"]["scorer"]["llm_attempted"] is False
        assert result["route_telemetry"]["matched_pattern_id"] == "pattern-2"
        assert result["route_telemetry"]["matched_by"] == "semantic"

    async def test_project_context_partial_task_id_routes_to_task_lookup_without_llm(self, monkeypatch):
        called: list[tuple[str, dict]] = []

        async def forbidden_disambiguate(**kwargs):
            raise AssertionError("partial task id is a structural signal and should not call LLM")

        async def fake_execute(tool_name: str, args: dict, api_base: str, session_id=None):
            called.append((tool_name, args))
            return json.dumps({"items": [{"task_id": "382e7306-cb61-46ee-8398-bc0a9bdfd9ef"}]})

        monkeypatch.setattr(mcp_sse, "_facade_llm_disambiguate", forbidden_disambiguate)
        monkeypatch.setattr(mcp_sse, "_execute_tool", fake_execute)
        result = await mcp_sse._build_project_context_payload(
            "http://test",
            {"project": "alpha", "intent": "382e7306"},
        )

        assert result["selected_route"]["tool"] == "list_artifacts"
        assert result["selected_route"]["intent_type"] == "task_lookup"
        assert "Partial task_id detected" in result["warnings"][0]
        assert called[0] == ("list_artifacts", {"project": "alpha", "type": "task", "limit": 50})

    async def test_project_context_diagnostic_response_is_plain_text_route_block(self, monkeypatch):
        async def fake_project_context(api_base: str, args: dict, session_id=None):
            return {
                "status": "executed",
                "facade": "project_context",
                "project": "alpha",
                "intent": "382e7306",
                "action_status": "executed",
                "selected_route": {
                    "tool": "list_artifacts",
                    "intent_type": "task_lookup",
                    "mutating": False,
                    "confidence": 0.8,
                    "reason": "A partial task-id-like token was provided.",
                    "scorer": {
                        "backend_requested": "auto",
                        "backend_used": "lexical",
                        "llm_attempted": False,
                        "fallback_reason": "",
                    },
                },
                "result": {"items": [{"task_id": "382e7306-cb61-46ee-8398-bc0a9bdfd9ef"}]},
                "route_telemetry": {
                    "scorer_backend": "lexical",
                    "fallback_used": False,
                    "fallback_reason": "",
                    "matched_pattern_id": "",
                    "matched_pattern_score": None,
                    "matched_by": "",
                    "warnings": ["Partial task_id detected"],
                },
                "warnings": ["Partial task_id detected"],
                "next_safe_action": "Continue from the executed route result.",
            }

        monkeypatch.setattr(mcp_sse, "_build_project_context_payload", fake_project_context)
        text = await mcp_sse._execute_tool(
            "project_context",
            {"project": "alpha", "intent": "382e7306", "diagnostic": True},
            "http://test",
        )

        assert text.startswith("Mnemoforge route diagnostic\n")
        assert "facade=project_context" in text
        assert "route.tool=list_artifacts" in text
        assert "route.intent_type=task_lookup" in text
        assert "scorer.backend_requested=auto" in text
        assert "scorer.backend_used=lexical" in text
        assert "scorer.llm_attempted=false" in text
        assert "telemetry.scorer_backend=lexical" in text
        assert "warnings=Partial task_id detected" in text
        assert "first_task_id=382e7306-cb61-46ee-8398-bc0a9bdfd9ef" in text

    async def test_ask_project_routes_partial_task_id_to_project_context_answer(self, monkeypatch):
        original_execute = mcp_sse._execute_tool
        calls: list[tuple[str, dict]] = []

        async def fake_execute(tool_name: str, args: dict, api_base: str, session_id=None):
            calls.append((tool_name, args))
            return (
                "Mnemoforge answer\n"
                "Answer: Found task 382e7306-cb61-46ee-8398-bc0a9bdfd9ef.\n"
                "task_id=382e7306-cb61-46ee-8398-bc0a9bdfd9ef"
            )

        monkeypatch.setattr(mcp_sse, "_execute_tool", fake_execute)
        text = await original_execute(
            "ask_project",
            {"project": "alpha", "question": "what is task 382e7306?"},
            "http://test",
        )

        assert text.startswith("Mnemoforge answer\n")
        assert "task_id=382e7306-cb61-46ee-8398-bc0a9bdfd9ef" in text
        assert calls[0][0] == "project_context"
        assert calls[0][1]["response_format"] == "answer"
        assert calls[0][1]["intent"] == "what is task 382e7306?"
        assert "allow_mutation" not in calls[0][1]

    async def test_ask_project_task_id_structural_route_beats_stale_learned_pattern(self, monkeypatch):
        original_execute = mcp_sse._execute_tool
        disabled: list[tuple[str, str]] = []
        calls: list[tuple[str, dict]] = []

        class StaleAskProjectPatternStore:
            def match(self, **kwargs):
                return {
                    "pattern_id": "stale-ask-project-task-route",
                    "intent_type": "project_work",
                    "tool": "project_work",
                    "confidence": 0.91,
                    "reason": "Matched stale next-task route.",
                    "backend_used": "learned_semantic",
                    "score": 0.88,
                    "matched_by": "semantic",
                }

            def disable_pattern(self, pattern_id: str, *, reason: str, metadata: dict | None = None):
                disabled.append((pattern_id, reason))
                return True

        async def fake_execute(tool_name: str, args: dict, api_base: str, session_id=None):
            calls.append((tool_name, args))
            return (
                "Mnemoforge answer\n"
                "Answer: Found task f6c44417-48b0-40fb-944c-3194fc20fad6.\n"
                "task_id=f6c44417-48b0-40fb-944c-3194fc20fad6"
            )

        monkeypatch.setattr(mcp_sse, "get_route_pattern_store", lambda: StaleAskProjectPatternStore())
        monkeypatch.setattr(mcp_sse, "_execute_tool", fake_execute)
        text = await original_execute(
            "ask_project",
            {
                "project": "mnemoforge",
                "question": "Детали задачи с ID: f6c44417-48b0-40fb-944c-3194fc20fad6",
            },
            "http://test",
        )

        assert "task_id=f6c44417-48b0-40fb-944c-3194fc20fad6" in text
        assert calls[0][0] == "project_context"
        assert calls[0][1]["intent"] == "Детали задачи с ID: f6c44417-48b0-40fb-944c-3194fc20fad6"
        assert disabled == [("stale-ask-project-task-route", "conflicts_with_structural_route")]

    async def test_ask_project_routes_readiness_question_to_project_context(self, monkeypatch):
        original_execute = mcp_sse._execute_tool
        calls: list[tuple[str, dict]] = []

        async def fake_execute(tool_name: str, args: dict, api_base: str, session_id=None):
            calls.append((tool_name, args))
            return "Mnemoforge answer\nAnswer: Project readiness route executed."

        monkeypatch.setattr(mcp_sse, "_execute_tool", fake_execute)
        text = await original_execute(
            "ask_project",
            {"project": "alpha", "question": "can this repo be used yet"},
            "http://test",
        )

        assert "Project readiness" in text
        assert calls[0][0] == "project_context"
        assert calls[0][1]["response_format"] == "answer"

    async def test_ask_project_routes_next_priority_to_project_work(self, monkeypatch):
        original_execute = mcp_sse._execute_tool
        calls: list[tuple[str, dict]] = []

        async def fake_execute(tool_name: str, args: dict, api_base: str, session_id=None):
            calls.append((tool_name, args))
            return "Mnemoforge answer\nAnswer: project_work executed route list_open_tasks."

        monkeypatch.setattr(mcp_sse, "_execute_tool", fake_execute)
        text = await original_execute(
            "ask_project",
            {"project": "alpha", "question": "what should I do next?"},
            "http://test",
        )

        assert "project_work executed" in text
        assert calls[0][0] == "project_work"
        assert calls[0][1]["allow_mutation"] is False
        assert calls[0][1]["response_format"] == "answer"

    async def test_ask_project_accepts_query_alias_and_routes_to_project_work(self, monkeypatch):
        original_execute = mcp_sse._execute_tool
        calls: list[tuple[str, dict]] = []

        async def fake_execute(tool_name: str, args: dict, api_base: str, session_id=None):
            calls.append((tool_name, args))
            return "Mnemoforge answer\nAnswer: project_work executed route list_open_tasks."

        monkeypatch.setattr(mcp_sse, "_execute_tool", fake_execute)
        text = await original_execute(
            "ask_project",
            {"project": "alpha", "query": "What are my active/open tasks right now? List all open tasks."},
            "http://test",
        )

        assert "project_work executed" in text
        assert calls[0][0] == "project_work"
        assert calls[0][1]["intent"] == "What are my active/open tasks right now? List all open tasks."

    async def test_ask_project_snake_case_query_routes_to_project_work(self, monkeypatch):
        original_execute = mcp_sse._execute_tool
        calls: list[tuple[str, dict]] = []

        async def fake_execute(tool_name: str, args: dict, api_base: str, session_id=None):
            calls.append((tool_name, args))
            return "Mnemoforge answer\nAnswer: project_work executed route list_open_tasks."

        monkeypatch.setattr(mcp_sse, "_execute_tool", fake_execute)
        text = await original_execute(
            "ask_project",
            {"project": "alpha", "query": "list_open_tasks"},
            "http://test",
        )

        assert "project_work executed" in text
        assert calls[0][0] == "project_work"
        assert calls[0][1]["intent"] == "list_open_tasks"

    async def test_ask_project_uses_learned_route_pattern(self, monkeypatch):
        original_execute = mcp_sse._execute_tool
        calls: list[tuple[str, dict]] = []

        async def fake_execute(tool_name: str, args: dict, api_base: str, session_id=None):
            calls.append((tool_name, args))
            return "Mnemoforge answer\nAnswer: project_verify executed route get_task_execution_context."

        monkeypatch.setattr(
            mcp_sse,
            "_learned_route_match",
            lambda **kwargs: {
                "tool": "project_verify",
                "reason": "Matched a learned route pattern.",
                "confidence": 0.93,
                "backend_used": "learned_semantic",
                "pattern_id": "pat-1",
                "score": 0.88,
                "matched_by": "semantic",
            },
        )
        monkeypatch.setattr(mcp_sse, "_execute_tool", fake_execute)
        text = await original_execute(
            "ask_project",
            {"project": "alpha", "question": "check this repo health for me"},
            "http://test",
        )

        assert "project_verify executed" in text
        assert calls[0][0] == "project_verify"
        assert calls[0][1]["allow_mutation"] is False

    async def test_ask_project_routine_reduction_footer_is_self_contained(self, monkeypatch):
        original_execute = mcp_sse._execute_tool

        async def fake_execute(tool_name: str, args: dict, api_base: str, session_id=None):
            return (
                "Mnemoforge answer\n"
                "Answer: Next useful project action is First task.\n"
                "task_id=task-1"
            )

        monkeypatch.setattr(mcp_sse, "_execute_tool", fake_execute)
        text = await original_execute(
            "ask_project",
            {
                "project": "alpha",
                "question": "what should I do next?",
                "evaluation_footer": "routine_reduction",
            },
            "http://test",
        )

        assert text.startswith("Mnemoforge answer\n")
        assert text.rstrip().endswith("ROUTINE_REDUCTION_OK = yes")

    async def test_ask_project_mutating_request_stays_guarded(self, monkeypatch):
        original_execute = mcp_sse._execute_tool
        calls: list[tuple[str, dict]] = []

        async def fake_execute(tool_name: str, args: dict, api_base: str, session_id=None):
            calls.append((tool_name, args))
            return "Mnemoforge answer\nAnswer: No mutation was executed."

        monkeypatch.setattr(mcp_sse, "_execute_tool", fake_execute)
        text = await original_execute(
            "ask_project",
            {"project": "alpha", "question": "save checkpoint for this task"},
            "http://test",
        )

        assert "No mutation was executed" in text
        assert calls[0][0] == "project_capture"
        assert calls[0][1]["allow_mutation"] is False

    async def test_ask_project_diagnostic_explains_selected_facade(self, monkeypatch):
        original_execute = mcp_sse._execute_tool

        async def fake_execute(tool_name: str, args: dict, api_base: str, session_id=None):
            return "Mnemoforge answer\nAnswer: Found task 382e7306."

        monkeypatch.setattr(mcp_sse, "_execute_tool", fake_execute)
        text = await original_execute(
            "ask_project",
            {"project": "alpha", "question": "what is task 382e7306?", "response_format": "diagnostic"},
            "http://test",
        )

        assert text.startswith("Mnemoforge ask_project diagnostic\n")
        assert "selected_facade=project_context" in text
        assert "response_format=diagnostic" in text
        assert "Question contains a full or partial task id" in text

    async def test_project_context_answer_response_is_final_answer_shaped(self, monkeypatch):
        async def fake_project_context(api_base: str, args: dict, session_id=None):
            return {
                "status": "executed",
                "facade": "project_context",
                "project": "alpha",
                "intent": "382e7306",
                "action_status": "executed",
                "selected_route": {
                    "tool": "list_artifacts",
                    "intent_type": "task_lookup",
                    "mutating": False,
                    "confidence": 0.8,
                    "reason": "A partial task-id-like token was provided.",
                    "scorer": {
                        "backend_requested": "auto",
                        "backend_used": "lexical",
                        "llm_attempted": False,
                        "fallback_reason": "",
                    },
                },
                "result": {
                    "items": [
                        {
                            "task_id": "6f8e5a1d-811a-4bf5-8469-39799ddf9266",
                            "title": "Add ask_project human-facing expert facade",
                            "status": "open",
                            "artifact_key": "task:alpha:6f8e5a1d-811a-4bf5-8469-39799ddf9266",
                        },
                        {
                            "task_id": "382e7306-cb61-46ee-8398-bc0a9bdfd9ef",
                            "title": "Add shared semantic or LLM route matching",
                            "status": "done",
                            "artifact_key": "task:alpha:382e7306-cb61-46ee-8398-bc0a9bdfd9ef",
                        }
                    ]
                },
                "route_telemetry": {
                    "scorer_backend": "lexical",
                    "fallback_used": False,
                    "warnings": ["Partial task_id detected"],
                },
                "warnings": ["Partial task_id detected"],
                "next_safe_action": "Continue from the executed route result.",
                "executed": True,
            }

        monkeypatch.setattr(mcp_sse, "_build_project_context_payload", fake_project_context)
        text = await mcp_sse._execute_tool(
            "project_context",
            {"project": "alpha", "intent": "382e7306", "response_format": "answer"},
            "http://test",
        )

        assert text.startswith("Mnemoforge answer\n")
        assert "Answer: Found task 382e7306-cb61-46ee-8398-bc0a9bdfd9ef." in text
        assert "task_id=382e7306-cb61-46ee-8398-bc0a9bdfd9ef" in text
        assert "title=Add shared semantic or LLM route matching" in text
        assert "task_status=done" in text
        assert "warnings=Partial task_id detected" in text
        assert "route=list_artifacts" not in text
        assert "intent_type=task_lookup" not in text
        assert "scorer_backend=lexical" not in text
        assert "selected_route" not in text
        assert "route_candidates" not in text

    async def test_project_capture_answer_response_does_not_authorize_guarded_mutation(self, monkeypatch):
        async def fake_project_capture(api_base: str, args: dict, session_id=None):
            return {
                "status": "planned",
                "facade": "project_capture",
                "project": "alpha",
                "intent": "save checkpoint",
                "action_status": "plan",
                "selected_route": {
                    "tool": "record_work_result",
                    "intent_type": "record_work_result",
                    "mutating": True,
                    "confidence": 0.88,
                    "reason": "Guarded mutation.",
                },
                "result": None,
                "route_telemetry": {"scorer_backend": "lexical", "warnings": []},
                "warnings": ["Selected project_capture route is mutating; set allow_mutation=true only after reviewing submit_payload."],
                "next_safe_action": "Review the plan before setting allow_mutation=true.",
                "executed": False,
            }

        monkeypatch.setattr(mcp_sse, "_build_project_capture_payload", fake_project_capture)
        text = await mcp_sse._execute_tool(
            "project_capture",
            {"project": "alpha", "intent": "save checkpoint", "answer": True},
            "http://test",
        )

        assert "Answer: No mutation was executed." in text
        assert "next_safe_action=Review the plan before setting allow_mutation=true." in text
        assert "route=record_work_result" not in text

    async def test_project_context_executes_reconstruction_bundle(self, monkeypatch):
        called: list[tuple[str, dict]] = []

        async def fake_execute(tool_name: str, args: dict, api_base: str, session_id=None):
            called.append((tool_name, args))
            return "Reconstruction bundle\nsource_code_required=false"

        monkeypatch.setattr(mcp_sse, "_execute_tool", fake_execute)
        result = await mcp_sse._build_project_context_payload(
            "http://test",
            {"project": "alpha", "intent": "give source loss reconstruction context", "detail": "compact"},
        )

        assert result["status"] == "executed"
        assert result["selected_route"]["tool"] == "get_project_reconstruction_bundle"
        assert called[0][1]["project_id"] == "alpha"
        assert "source_code_required=false" in result["result"]

    async def test_project_verify_routes_tests_to_execution_context(self, monkeypatch):
        called: list[tuple[str, dict]] = []

        async def fake_execute(tool_name: str, args: dict, api_base: str, session_id=None):
            called.append((tool_name, args))
            return json.dumps({"project": args["project"], "state": args["state"], "risk_controls": ["docker contour"]})

        monkeypatch.setattr(mcp_sse, "_execute_tool", fake_execute)
        result = await mcp_sse._build_project_verify_payload(
            "http://test",
            {
                "project": "alpha",
                "task_id": "task-1",
                "intent": "run tests for this change",
                "changed_files": ["app/routers/mcp_sse.py"],
            },
        )

        assert result["status"] == "executed"
        assert result["selected_route"]["tool"] == "get_task_execution_context"
        assert result["selected_route"]["intent_type"] == "verification_context"
        assert called[0][1]["state"] == "verification"
        assert called[0][1]["changed_files"] == ["app/routers/mcp_sse.py"]
        guidance = result["project_verify_guidance"]
        assert guidance["verification_contour_ref"] == "verification_contour:alpha:verification"
        assert guidance["restart_window_ref"] == "restart_window:alpha:live_validation"
        assert guidance["live_boundary_ref"] == "live_boundary:alpha:verification"
        assert "restart_window_seconds" not in guidance
        assert "docker_test_contour" not in guidance
        assert result["selected_route"]["scorer"]["backend_used"] == "lexical"
        assert result["route_telemetry"]["scorer_backend"] == "lexical"
        assert result["semantic_rules"]["context_type"] in {"post_implementation", "post_validation"}
        assert "fallback_used" in result["semantic_rules"]

    async def test_project_verify_blocks_host_pytest_when_semantic_precondition_fails(self, monkeypatch):
        called: list[tuple[str, dict]] = []

        async def fake_execute(tool_name: str, args: dict, api_base: str, session_id=None):
            called.append((tool_name, args))
            return json.dumps({"ok": True})

        monkeypatch.setattr(mcp_sse, "_execute_tool", fake_execute)
        result = await mcp_sse._build_project_verify_payload(
            "http://test",
            {
                "project": "alpha",
                "task_id": "task-1",
                "intent": "run tests with pytest -q",
                "command": "pytest tests/test_mcp_sse.py -q",
            },
        )

        assert result["status"] == "executed"
        assert result["result"]["status"] == "conflict"
        assert result["result"]["error"] == "rule_precondition_failed:declared_verification_contour"
        assert called == []

    async def test_project_verify_auto_backend_falls_back_to_lexical_when_llm_fails(self, monkeypatch):
        async def broken_disambiguate(*, facade: str, text: str, args: dict, candidates: list[dict], catalog: tuple[dict, ...]):
            assert facade == "project_verify"
            raise RuntimeError("classifier unavailable")

        async def fake_execute(tool_name: str, args: dict, api_base: str, session_id=None):
            return json.dumps({"project": args["project"], "state": args["state"]})

        monkeypatch.setattr(mcp_sse, "_facade_llm_disambiguate", broken_disambiguate)
        monkeypatch.setattr(mcp_sse, "_execute_tool", fake_execute)
        result = await mcp_sse._build_project_verify_payload(
            "http://test",
            {"project": "alpha", "intent": "unclear verification thing", "scorer_backend": "auto"},
        )

        assert result["selected_route"]["scorer"]["backend_used"] == "lexical"
        assert result["selected_route"]["scorer"]["llm_attempted"] is True
        assert "classifier unavailable" in result["selected_route"]["scorer"]["fallback_reason"]
        assert result["route_telemetry"]["fallback_used"] is True

    async def test_project_verify_health_check_uses_health_surface(self, monkeypatch):
        called: list[tuple[str, dict]] = []

        async def fake_execute(tool_name: str, args: dict, api_base: str, session_id=None):
            called.append((tool_name, args))
            return json.dumps({"status": "ok"})

        monkeypatch.setattr(mcp_sse, "_execute_tool", fake_execute)
        result = await mcp_sse._build_project_verify_payload(
            "http://test",
            {"project": "alpha", "intent": "healthcheck"},
        )

        assert result["selected_route"]["tool"] == "memory_health"
        assert result["selected_route"]["intent_type"] == "health_check"
        assert called[0] == ("memory_health", {})

    async def test_project_capture_plans_work_result_without_mutation_by_default(self, monkeypatch):
        async def forbidden_execute(tool_name: str, args: dict, api_base: str, session_id=None):
            raise AssertionError("guarded capture route should not execute without allow_mutation")

        monkeypatch.setattr(mcp_sse, "_execute_tool", forbidden_execute)
        result = await mcp_sse._build_project_capture_payload(
            "http://test",
            {
                "project": "alpha",
                "task_id": "task-1",
                "intent": "save checkpoint",
                "summary": "Facade slice is complete.",
                "changed_files": ["app/routers/mcp_sse.py"],
            },
        )

        assert result["status"] == "planned"
        assert result["action_status"] == "needs_confirmation"
        assert result["selected_route"]["tool"] == "record_work_result"
        assert result["submit_payload"]["summary"] == "Facade slice is complete."
        assert result["route_telemetry"]["facade"] == "project_capture"
        assert result["route_telemetry"]["underlying_tool"] == "record_work_result"
        assert result["route_telemetry"]["scorer_backend"] == "lexical"
        assert result["route_telemetry"]["guardrail_triggered"] is True
        assert result["agent_action"]["recommended_next_call"]["arguments"]["allow_mutation"] is True
        assert result["agent_action"]["do_not_call"] == ["record_work_result", "record_task_checkpoint", "record_stenographer_span"]

    async def test_project_capture_executes_review_only_clerk_draft(self, monkeypatch):
        called: list[tuple[str, dict]] = []

        async def fake_execute(tool_name: str, args: dict, api_base: str, session_id=None):
            called.append((tool_name, args))
            return json.dumps({"draft_id": "draft-1", "mutates_memory": False})

        monkeypatch.setattr(mcp_sse, "_execute_tool", fake_execute)
        result = await mcp_sse._build_project_capture_payload(
            "http://test",
            {
                "project": "alpha",
                "task_id": "task-1",
                "intent": "draft checkpoint from notes",
                "raw_notes": "Implementation finished; tests pending.",
            },
        )

        assert result["status"] == "executed"
        assert result["selected_route"]["tool"] == "clerk_draft_report"
        assert result["selected_route"]["intent_type"] == "draft_capture"
        assert result["selected_route"]["scorer"]["backend_used"] == "lexical"
        assert called[0][0] == "clerk_draft_report"
        assert called[0][1]["raw_notes"] == "Implementation finished; tests pending."

    async def test_record_work_result_uses_provided_task_and_records_memory_checkpoint(self, monkeypatch):
        posted: list[tuple[str, dict]] = []
        lease_store, work_token = _install_task_claim_store(monkeypatch)

        async def fake_post(api_base: str, path: str, payload: dict):
            posted.append((path, payload))
            if path == "/memories":
                return {"id": "memory-1"}
            if path == "/project/tasks/task-1/changes":
                return {"id": "change-1", "task_id": "task-1"}
            raise AssertionError(f"unexpected POST path: {path}")

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        try:
            result = await mcp_sse._execute_tool(
                "record_work_result",
                {
                    "project": "alpha",
                    "task_id": "task-1",
                    "summary": "Implemented high-level closeout facade.",
                    "changed_files": ["app/routers/mcp_sse.py"],
                    "verification": ["pytest tests/test_mcp_sse.py passed"],
                    "session_id": "sess-1",
                    "acted_by": "codex",
                    "work_token": work_token,
                },
                "http://test",
            )
        finally:
            lease_store.close()

        data = json.loads(result)
        assert data["route"] == ["memory", "task_checkpoint"]
        assert data["target"]["artifact_key"] == "task:alpha:task-1"
        assert posted[0][0] == "/memories"
        assert "Implemented high-level closeout facade." in posted[0][1]["content"]
        assert posted[1][0] == "/project/tasks/task-1/changes"
        assert "task_checkpoint" in posted[1][1]["tags"]
        assert data["checkpoint"]["stage_evidence"] == "checkpoint:change-1"

    async def test_record_work_result_auto_matches_newest_open_task(self, monkeypatch):
        requested: list[str] = []
        posted: list[tuple[str, dict]] = []
        lease_store, work_token = _install_task_claim_store(monkeypatch, task_id="auto-task")

        async def fake_get(api_base: str, path: str):
            requested.append(path)
            if path.startswith("/artifacts?"):
                return {
                    "items": [
                        {
                            "artifact_key": "task:alpha:auto-task",
                            "linked_artifact_key": "improvement:alpha:auto-task",
                        }
                    ]
                }
            raise AssertionError(f"unexpected GET path: {path}")

        async def fake_post(api_base: str, path: str, payload: dict):
            posted.append((path, payload))
            if path == "/memories":
                return {"id": "memory-1"}
            if path == "/project/tasks/auto-task/changes":
                return {"id": "change-2", "task_id": "auto-task"}
            raise AssertionError(f"unexpected POST path: {path}")

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        try:
            result = await mcp_sse._execute_tool(
                "record_work_result",
                {"project": "alpha", "summary": "Recorded result on the current task.", "session_id": "sess-1", "acted_by": "codex", "work_token": work_token},
                "http://test",
            )
        finally:
            lease_store.close()

        data = json.loads(result)
        assert requested[0].startswith("/artifacts?project=alpha&status=open&limit=1&type=task")
        assert data["target"]["target_source"] == "newest_open_task"
        assert data["target"]["task_id"] == "auto-task"
        assert posted[1][0] == "/project/tasks/auto-task/changes"

    async def test_record_work_result_can_skip_auto_match_and_create_improvement(self, monkeypatch):
        posted: list[tuple[str, dict]] = []

        async def forbidden_get(api_base: str, path: str):
            raise AssertionError("skip_auto_task_match should not query newest open task")

        async def fake_post(api_base: str, path: str, payload: dict):
            posted.append((path, payload))
            if path == "/memories":
                return {"id": "memory-1"}
            if path == "/improvements":
                return {"id": "issue-1", "title": payload["title"], "status": "open"}
            raise AssertionError(f"unexpected POST path: {path}")

        monkeypatch.setattr(mcp_sse, "_get", forbidden_get)
        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        result = await mcp_sse._execute_tool(
            "record_work_result",
            {
                "project": "alpha",
                "title": "Add task leasing",
                "summary": "Create DB-backed task leases for multi-agent sessions.",
                "create_issue_if_unmatched": True,
                "skip_auto_task_match": True,
            },
            "http://test",
        )

        data = json.loads(result)
        assert data["route"] == ["memory", "improvement"]
        assert data["target"]["target_source"] == "unmatched"
        assert data["created_issue"]["id"] == "issue-1"
        assert posted[0][1]["project"] == "alpha"
        assert posted[1][0] == "/improvements"
        assert posted[1][1]["title"] == "Add task leasing"
        assert "source_memory:memory-1" in posted[1][1]["tags"]

    async def test_record_work_result_falls_back_to_memory_only_when_unmatched(self, monkeypatch):
        async def fake_get(api_base: str, path: str):
            return {"items": []}

        async def fake_post(api_base: str, path: str, payload: dict):
            if path == "/memories":
                return {"id": "memory-only"}
            raise AssertionError(f"unexpected POST path: {path}")

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        result = await mcp_sse._execute_tool(
            "record_work_result",
            {"project": "alpha", "summary": "No artifact was available."},
            "http://test",
        )

        data = json.loads(result)
        assert data["route"] == ["memory"]
        assert data["memory"]["id"] == "memory-only"
        assert "memory-only result" in data["warnings"][0]

    async def test_record_work_result_uses_explicit_clerk_draft_when_stenographer_spans_exist(self, monkeypatch):
        from pathlib import Path
        from app.services import checkpoint_draft_service as draft_mod
        from app.services import stenographer_service as stenographer_mod

        store = stenographer_mod.StenographerStore(Path(":memory:"))
        lease_store, work_token = _install_task_claim_store(monkeypatch)
        monkeypatch.setattr(stenographer_mod, "_STORE", store)
        posted: list[tuple[str, dict]] = []
        draft_calls: list[dict] = []

        class FakeDraft:
            def model_dump(self, mode: str = "json"):
                return {
                    "draft_id": "draft-1",
                    "version": 1,
                    "status": "drafted",
                    "validation_report": {"can_approve": True},
                    "source_span_ids": ["span-1"],
                }

        async def fake_draft(payload: dict, llm_gateway):
            draft_calls.append(payload)
            return FakeDraft()

        async def fake_post(api_base: str, path: str, payload: dict):
            posted.append((path, payload))
            if path == "/memories":
                return {"id": "memory-1"}
            raise AssertionError(f"unexpected POST path: {path}")

        monkeypatch.setattr(draft_mod, "draft_checkpoint_from_spans", fake_draft)
        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        try:
            store.start_work_session(
                project="alpha",
                task_id="task-1",
                agent_id="codex",
                session_id="sess-1",
                work_id="work-1",
            )
            store.record_span(
                project="alpha",
                task_id="task-1",
                agent_id="codex",
                session_id="sess-1",
                work_id="work-1",
                kind="verification",
                source="pytest",
                content="pytest passed",
            )
            result = await mcp_sse._execute_tool(
                "record_work_result",
                {
                    "project": "alpha",
                    "task_id": "task-1",
                    "work_id": "work-1",
                    "agent_id": "codex",
                    "session_id": "sess-1",
                    "work_token": work_token,
                    "summary": "Closeout with stenographer evidence.",
                    "use_clerk": True,
                },
                "http://test",
            )
        finally:
            store.close()
            lease_store.close()

        data = json.loads(result)
        assert data["status"] == "drafted"
        assert data["route"] == ["memory", "clerk_draft"]
        assert data["clerk_draft"]["draft_id"] == "draft-1"
        assert posted == [("/memories", posted[0][1])]
        assert draft_calls[0]["work_id"] == "work-1"
        assert "review-only clerk draft" in data["warnings"][0]

    async def test_record_work_result_records_direct_checkpoint_by_default_with_stenographer_spans(self, monkeypatch):
        from pathlib import Path
        from app.services import stenographer_service as stenographer_mod

        store = stenographer_mod.StenographerStore(Path(":memory:"))
        lease_store, work_token = _install_task_claim_store(monkeypatch)
        monkeypatch.setattr(stenographer_mod, "_STORE", store)
        posted: list[tuple[str, dict]] = []

        async def fake_post(api_base: str, path: str, payload: dict):
            posted.append((path, payload))
            if path == "/memories":
                return {"id": "memory-1"}
            if path == "/project/tasks/task-1/changes":
                return {"id": "change-1"}
            raise AssertionError(f"unexpected POST path: {path}")

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        try:
            store.start_work_session(
                project="alpha",
                task_id="task-1",
                agent_id="codex",
                session_id="sess-1",
                work_id="work-1",
            )
            store.record_span(
                project="alpha",
                task_id="task-1",
                agent_id="codex",
                session_id="sess-1",
                work_id="work-1",
                kind="verification",
                source="pytest",
                content="pytest passed",
            )
            result = await mcp_sse._execute_tool(
                "record_work_result",
                {
                    "project": "alpha",
                    "task_id": "task-1",
                    "work_id": "work-1",
                    "agent_id": "codex",
                    "session_id": "sess-1",
                    "work_token": work_token,
                    "summary": "Closeout with stenographer evidence.",
                },
                "http://test",
            )
        finally:
            store.close()
            lease_store.close()

        data = json.loads(result)
        assert data["status"] == "recorded"
        assert data["route"] == ["memory", "task_checkpoint"]
        assert data["checkpoint"]["stage_evidence"] == "checkpoint:change-1"
        assert [path for path, _ in posted] == ["/memories", "/project/tasks/task-1/changes"]

    async def test_clerk_draft_report_from_raw_notes_uses_memory_scribe_without_mutating(self, monkeypatch):
        posted: list[tuple[str, dict]] = []

        async def fake_post(api_base: str, path: str, payload: dict):
            posted.append((path, payload))
            return {"id": "unexpected"}

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "clerk_draft_report",
            {
                "project": "alpha",
                "task_id": "task-1",
                "stage": "completed",
                "status": "done",
                "use_llm": False,
                "raw_notes": "\n".join(
                    [
                        "Summary: Clerk structured raw notes.",
                        "Verification: pytest tests/test_mcp_sse.py passed",
                        "Changed files: app/routers/mcp_sse.py",
                        "Next step: Approve the report draft.",
                    ]
                ),
            },
            "http://test",
        )

        data = json.loads(result)
        assert posted == []
        assert data["clerk_mode"] == "raw_notes"
        assert data["mutates_memory"] is False
        assert data["record_task_checkpoint_args"]["source"] == "memory_scribe"

    async def test_upsert_knowledge_tree_node_tool_posts_structured_payload(self, monkeypatch):
        posted: list[tuple[str, dict]] = []

        async def fake_post(api_base: str, path: str, payload: dict):
            posted.append((path, payload))
            return {
                "created": True,
                "node_id": "node-1",
                "topic_path": payload["topic_path"],
                "node": {
                    "id": "node-1",
                    "title": payload["title"],
                    "topic_path": payload["topic_path"],
                    "meta_json": {"structured_knowledge": {"responsibility": payload["responsibility"]}},
                },
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        result = await mcp_sse._execute_tool(
            "upsert_knowledge_tree_node",
            {
                "topic_path": "mnemoforge/architecture/mcp/compact-discovery",
                "title": "Compact MCP Discovery",
                "responsibility": "Expose a small MCP catalog before the full flat tool list.",
                "runtime_entrypoints": ["initialize", "tools/list"],
                "projection_targets": ["README.md"],
                "evidence_refs": ["checkpoint:abc"],
            },
            "http://test",
        )

        assert posted[0][0] == "/tree/upsert-by-path"
        assert posted[0][1]["topic_path"] == "mnemoforge/architecture/mcp/compact-discovery"
        assert posted[0][1]["type"] == "area"
        assert posted[0][1]["source"] == "mcp_upsert_knowledge_tree_node"
        assert '"created": true' in result
        assert '"stage": "testing"' in result

    def test_rule_candidate_tools_are_exposed(self):
        names = {tool["name"] for tool in mcp_sse.TOOLS}
        assert {"project_rule_candidates_from_stenography", "list_rule_candidates", "get_rule_candidate_review_packet"} <= names

    async def test_project_rule_candidates_tool_posts_projection_request(self, monkeypatch):
        posted: list[tuple[str, dict]] = []

        async def fake_post(api_base: str, path: str, payload: dict):
            posted.append((path, payload))
            return {
                "scanned_spans": 1,
                "created_candidates": 1,
                "skipped_spans": 0,
                "errors": [],
                "last_processed_timestamp": 1.0,
                "candidates": [],
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        result = await mcp_sse._execute_tool(
            "project_rule_candidates_from_stenography",
            {"project": "alpha", "limit": 25},
            "http://test",
        )

        assert posted == [("/laws/candidates/project-from-stenography", {"project": "alpha", "limit": 25})]
        assert '"created_candidates": 1' in result
        assert '"stage": "testing"' in result

    async def test_list_rule_candidates_tool_formats_candidates(self, monkeypatch):
        seen: dict[str, str] = {}

        async def fake_get(api_base: str, path: str):
            seen["path"] = path
            return {
                "total": 1,
                "items": [
                    {
                        "candidate_id": "candidate-1",
                        "project": "alpha",
                        "scope": "project",
                        "topic_path": "testing/contour",
                        "status": "candidate",
                        "statement": "Use Docker test contour.",
                        "source_span_id": "span-1",
                    }
                ],
            }

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        result = await mcp_sse._execute_tool(
            "list_rule_candidates",
            {"project": "alpha", "status": "candidate", "source_task_id": "task-1", "limit": 10},
            "http://test",
        )

        assert seen["path"] == "/laws/candidates?limit=10&project=alpha&status=candidate&source_task_id=task-1"
        assert "Use Docker test contour." in result
        assert "candidate_id=candidate-1" in result

    async def test_get_rule_candidate_review_packet_tool_posts_review_request(self, monkeypatch):
        posted: list[tuple[str, dict]] = []

        async def fake_post(api_base: str, path: str, payload: dict):
            posted.append((path, payload))
            return {
                "project": payload["project"],
                "total_candidates": 0,
                "items": [],
                "risk_controls": [],
                "next_actions": [],
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        result = await mcp_sse._execute_tool(
            "get_rule_candidate_review_packet",
            {"project": "alpha", "status": "candidate", "source_task_id": "task-1", "limit": 10, "max_matches": 3},
            "http://test",
        )

        assert posted == [
            (
                "/laws/candidates/review-packet",
                {
                    "project": "alpha",
                    "status": "candidate",
                    "source_task_id": "task-1",
                    "limit": 10,
                    "max_matches": 3,
                },
            )
        ]
        assert '"total_candidates": 0' in result
        assert '"stage": "testing"' in result

    async def test_review_rule_candidate_tool_posts_safe_action(self, monkeypatch):
        posted: list[tuple[str, dict]] = []

        async def fake_post(api_base: str, path: str, payload: dict):
            posted.append((path, payload))
            return {
                "candidate": {
                    "candidate_id": "candidate-1",
                    "status": "rejected",
                    "project": "alpha",
                    "scope": "project",
                    "marker_kind": "rule_project_candidate",
                    "statement": "Duplicate rule.",
                    "source_span_id": "span-1",
                    "created_at": "2026-04-29T00:00:00Z",
                    "updated_at": "2026-04-29T00:00:00Z",
                },
                "previous_status": "candidate",
                "new_status": "rejected",
                "action": payload["action"],
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        result = await mcp_sse._execute_tool(
            "review_rule_candidate",
            {
                "candidate_id": "candidate-1",
                "action": "reject",
                "reason": "Duplicate of active law.",
                "acted_by": "codex",
            },
            "http://test",
        )

        assert posted == [
            (
                "/laws/candidates/candidate-1/review",
                {
                    "action": "reject",
                    "reason": "Duplicate of active law.",
                    "acted_by": "codex",
                    "source": "mcp_rule_candidate_review",
                },
            )
        ]
        assert '"new_status": "rejected"' in result
        assert '"stage": "testing"' in result

    async def test_promote_rule_candidate_tool_posts_promotion_request(self, monkeypatch):
        posted: list[tuple[str, dict]] = []

        async def fake_post(api_base: str, path: str, payload: dict):
            posted.append((path, payload))
            return {
                "candidate": {
                    "candidate_id": "candidate-1",
                    "status": "suppressed",
                    "project": "alpha",
                    "scope": "canonical_candidate",
                    "marker_kind": "rule_canonical_candidate",
                    "statement": "Clarify task framing.",
                    "source_span_id": "span-1",
                    "promoted_law_id": "law-1",
                    "created_at": "2026-04-29T00:00:00Z",
                    "updated_at": "2026-04-29T00:00:00Z",
                },
                "law": {"id": "law-1", "title": payload["title"], "status": payload["status"]},
                "previous_status": "candidate",
                "new_status": "suppressed",
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        result = await mcp_sse._execute_tool(
            "promote_rule_candidate",
            {
                "candidate_id": "candidate-1",
                "title": "Clarify Task Framing Before Implementation",
                "target_scope": "principle",
                "status": "proposed",
                "reason": "No duplicate in review packet.",
                "acted_by": "codex",
            },
            "http://test",
        )

        assert posted == [
            (
                "/laws/candidates/candidate-1/promote",
                {
                    "title": "Clarify Task Framing Before Implementation",
                    "target_scope": "principle",
                    "status": "proposed",
                    "reason": "No duplicate in review packet.",
                    "acted_by": "codex",
                    "source": "mcp_rule_candidate_promotion",
                    "confirmation_source": "mcp_rule_candidate_promotion",
                },
            )
        ]
        assert '"promoted_law_id": "law-1"' in result
        assert '"stage": "testing"' in result

    async def test_revise_law_from_rule_candidate_tool_posts_revision_request(self, monkeypatch):
        posted: list[tuple[str, dict]] = []

        async def fake_post(api_base: str, path: str, payload: dict):
            posted.append((path, payload))
            return {
                "candidate": {
                    "candidate_id": "candidate-1",
                    "status": "revision_pending",
                    "project": "alpha",
                    "scope": "project",
                    "marker_kind": "rule_project_candidate",
                    "statement": "Clarify task framing.",
                    "source_span_id": "span-1",
                    "revised_law_id": payload["law_id"],
                    "created_at": "2026-04-29T00:00:00Z",
                    "updated_at": "2026-04-29T00:00:00Z",
                },
                "law": {"id": payload["law_id"], "status": "active", "candidate_revision": {}},
                "previous_status": "candidate",
                "new_status": "revision_pending",
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        result = await mcp_sse._execute_tool(
            "revise_law_from_rule_candidate",
            {
                "candidate_id": "candidate-1",
                "law_id": "law-1",
                "reason": "Candidate improves existing law.",
                "acted_by": "codex",
            },
            "http://test",
        )

        assert posted == [
            (
                "/laws/candidates/candidate-1/revise-law",
                {
                    "law_id": "law-1",
                    "reason": "Candidate improves existing law.",
                    "acted_by": "codex",
                    "source": "mcp_rule_candidate_law_revision",
                },
            )
        ]
        assert '"new_status": "revision_pending"' in result
        assert '"stage": "testing"' in result

    def test_draft_task_checkpoint_tool_is_review_only(self):
        tool = next(tool for tool in mcp_sse.TOOLS if tool["name"] == "draft_task_checkpoint")
        props = tool["inputSchema"]["properties"]
        assert "raw_notes" in tool["inputSchema"]["required"]
        assert props["use_llm"]["default"] is True
        assert "raw_notes" in props
        checkpoint_tool = next(tool for tool in mcp_sse.TOOLS if tool["name"] == "record_task_checkpoint")
        assert "next_step_scope" in checkpoint_tool["inputSchema"]["properties"]
        checkpoint_props = checkpoint_tool["inputSchema"]["properties"]
        assert checkpoint_props["checkpoint_mode"]["default"] == "standard"
        assert "lightweight" in checkpoint_props["checkpoint_mode"]["enum"]
        assert "stage_evidence_refs" in checkpoint_props

    def test_stenographer_work_session_tools_are_exposed(self):
        names = {tool["name"] for tool in mcp_sse.TOOLS}
        assert {
            "get_work_session_state",
            "start_work_session",
            "park_work_session",
            "resume_work_session",
            "end_work_session",
            "record_stenographer_span",
            "list_stenographer_spans",
        } <= names
        record_span_tool = next(tool for tool in mcp_sse.TOOLS if tool["name"] == "record_stenographer_span")
        assert "changed_files" in record_span_tool["inputSchema"]["properties"]["kind"]["enum"]

    def test_checkpoint_draft_approval_tools_are_exposed(self):
        names = {tool["name"] for tool in mcp_sse.TOOLS}
        assert {
            "draft_checkpoint_from_spans",
            "get_checkpoint_draft",
            "revise_checkpoint_draft",
            "approve_checkpoint_draft",
            "reject_checkpoint_draft",
        } <= names
        draft_tool = next(tool for tool in mcp_sse.TOOLS if tool["name"] == "draft_checkpoint_from_spans")
        props = draft_tool["inputSchema"]["properties"]
        assert props["preserve_evidence"]["default"] is False
        assert "preserve_evidence" in props["mode"]["enum"]

    def test_tool_feedback_schema_exposes_evaluation_envelope_fields(self):
        tool = next(tool for tool in mcp_sse.TOOLS if tool["name"] == "tool_feedback")
        props = tool["inputSchema"]["properties"]
        assert "scope" in props
        assert "what_was_tested" in props
        assert "expected_behavior" in props
        assert "observed_behavior" in props
        assert "next_action" in props
        assert "assessment" in props
        assert "should_promote" in props
        assert "confidence" in props

    async def test_live_observer_marks_project_activity(self, monkeypatch):
        fake_post = AsyncMock(return_value={})
        fake_qdrant = type("FakeQdrant", (), {"mark_used": AsyncMock()})()

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(dependencies, "get_qdrant", lambda: fake_qdrant)

        await mcp_sse._mcp_live_observe(
            "memory_search",
            {"project": "mnemoforge", "agent_id": "codex"},
            "http://test",
        )

        fake_qdrant.mark_used.assert_awaited_once_with([], project="mnemoforge")

    async def test_live_observer_emits_user_request_when_text_snippet_exists(self, monkeypatch):
        posted: list[dict] = []
        fake_qdrant = type("FakeQdrant", (), {"mark_used": AsyncMock()})()

        async def fake_post(api_base: str, path: str, payload: dict):
            assert path == "/learning/events"
            posted.append(payload)
            return {}

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(dependencies, "get_qdrant", lambda: fake_qdrant)

        await mcp_sse._mcp_live_observe(
            "memory_search",
            {
                "project": "mnemoforge",
                "agent_id": "codex",
                "query": "Need rollback checklist for Qdrant WAL cleanup after disk pressure.",
            },
            "http://test",
        )

        event_types = [item.get("event_type") for item in posted]
        assert "tool_call" in event_types
        assert "user_request" in event_types
        user_event = next(item for item in posted if item.get("event_type") == "user_request")
        assert "rollback checklist" in user_event["payload"]["request_text"].lower()
        fake_qdrant.mark_used.assert_awaited_once_with([], project="mnemoforge")

    async def test_session_observe_collects_dialogue_snippets(self, monkeypatch):
        class _FakeSessionStore:
            def __init__(self):
                self.ctx = {
                    "sess-1": {
                        "agent_id": "codex",
                        "tools_called": [],
                        "queries": [],
                        "skills_accessed": [],
                        "dialogue_snippets": [],
                    }
                }

            async def get_context(self, session_id: str):
                return self.ctx.get(session_id)

            async def set_context(self, session_id: str, ctx: dict):
                self.ctx[session_id] = ctx

        fake_store = _FakeSessionStore()
        from app.services import mcp_session_store

        monkeypatch.setattr(mcp_session_store, "get_session_store", lambda: fake_store)

        await mcp_sse._session_observe(
            "sess-1",
            "decompose_task_packet",
            {
                "task_description": "Need to split this migration into bounded packets with clear ownership and done criteria.",
                "packets": [
                    {"task_description": "Packet 1 handles schema setup and migration scripts."},
                    {"task_description": "Packet 2 handles verification and rollback checklist."},
                ],
            },
        )

        ctx = fake_store.ctx["sess-1"]
        assert ctx["tools_called"][-1]["tool"] == "decompose_task_packet"
        assert len(ctx["dialogue_snippets"]) >= 2

    async def test_auto_record_session_runs_dialogue_analysis_when_snippets_exist(self, monkeypatch):
        posted: list[tuple[str, dict]] = []

        async def fake_post(api_base: str, path: str, payload: dict):
            posted.append((path, payload))
            return {}

        monkeypatch.setattr(mcp_sse, "_post", fake_post)

        await mcp_sse._auto_record_session(
            {
                "api_base": "http://test/api/v1",
                "agent_id": "codex",
                "pack_id": "pack-1",
                "session_id": "sess-1",
                "connected_at": 0.0,
                "tools_called": [{"tool": "memory_search", "ts": 1.0}],
                "queries": ["qdrant rollback guide"],
                "skills_received": [],
                "dialogue_snippets": [
                    {
                        "tool": "memory_search",
                        "text": "Need rollback checklist for qdrant wal cleanup after disk pressure and segment corruption risk.",
                        "ts": 1.0,
                    }
                ],
            }
        )

        analyze_payloads = [payload for path, payload in posted if path == "/skills/dialogue/analyze"]
        assert len(analyze_payloads) == 1
        assert analyze_payloads[0]["agent_id"] == "codex"
        assert analyze_payloads[0]["session_id"] == "sess-1"
        assert "USER:" in analyze_payloads[0]["transcript"]

    async def test_memory_stats_tool_uses_memories_stats_path(self, monkeypatch):
        seen: dict[str, str] = {}

        async def fake_get(api_base: str, path: str):
            seen["path"] = path
            return {"total": 1}

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool("memory_stats", {}, "http://test")

        assert seen["path"] == "/memories/stats"
        assert '"total": 1' in result

    async def test_list_open_tasks_tool_uses_unified_artifacts_path(self, monkeypatch):
        seen: dict[str, str] = {}

        async def fake_get(api_base: str, path: str):
            seen["path"] = path
            return {"items": []}

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool("list_open_tasks", {"project": "alpha", "limit": 5}, "http://test")

        assert seen["path"] == "/artifacts?project=alpha&status=open&limit=5"
        assert "No open work items found." in result

    async def test_list_open_tasks_can_request_tasks_only(self, monkeypatch):
        seen: dict[str, str] = {}

        async def fake_get(api_base: str, path: str):
            seen["path"] = path
            return {"items": []}

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        await mcp_sse._execute_tool(
            "list_open_tasks",
            {"project": "alpha", "artifact_type": "task", "limit": 5},
            "http://test",
        )

        assert seen["path"] == "/artifacts?project=alpha&status=open&limit=5&type=task"

    async def test_list_open_tasks_surfaces_orphan_open_improvements(self, monkeypatch):
        async def fake_get(api_base: str, path: str):
            assert path == "/artifacts?project=alpha&status=open&limit=5"
            return {
                "items": [
                    {
                        "artifact_key": "improvement:alpha:imp-1",
                        "type": "improvement",
                        "id": "imp-1",
                        "title": "Fix memory attribution",
                        "description": "Raw memory must become visible governed work.",
                        "status": "open",
                        "linked_artifact_key": None,
                    }
                ]
            }

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool("list_open_tasks", {"project": "alpha", "limit": 5}, "http://test")

        assert "Available open work items: 1" in result
        assert "Fix memory attribution" in result
        assert "type=improvement" in result
        assert "Raw memory must become visible governed work." in result

    async def test_list_open_tasks_tool_url_encodes_datetime_filters(self, monkeypatch):
        seen: dict[str, str] = {}

        async def fake_get(api_base: str, path: str):
            seen["path"] = path
            return {"items": []}

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        await mcp_sse._execute_tool(
            "list_open_tasks",
            {
                "project": "alpha",
                "updated_after": "2026-04-17T00:00:00+04:00",
                "updated_before": "2026-04-18T00:00:00+04:00",
            },
            "http://test",
        )

        assert "updated_after=2026-04-17T00%3A00%3A00%2B04%3A00" in seen["path"]
        assert "updated_before=2026-04-18T00%3A00%3A00%2B04%3A00" in seen["path"]

    async def test_list_open_tasks_hides_claimed_tasks_by_default(self, monkeypatch):
        from pathlib import Path
        from app.services import task_lease_service as lease_mod

        store = lease_mod.TaskLeaseStore(Path(":memory:"))
        monkeypatch.setattr(lease_mod, "_STORE", store)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        async def fake_get(api_base: str, path: str):
            return {
                "items": [
                    {"artifact_key": "task:alpha:task-1", "task_id": "task-1", "title": "Busy task", "status": "open"},
                    {"artifact_key": "task:alpha:task-2", "task_id": "task-2", "title": "Free task", "status": "open"},
                ]
            }

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        try:
            store.claim(project="alpha", task_id="task-1", owner_agent="claude", session_id="sess-claude")
            result = await mcp_sse._execute_tool("list_open_tasks", {"project": "alpha"}, "http://test")

            assert "Available open tasks: 1" in result
            assert "Free task" in result
            assert "Busy task" not in result
            assert "Hidden claimed tasks: 1" in result
        finally:
            store.close()

    async def test_list_open_tasks_can_return_claimed_tasks(self, monkeypatch):
        from pathlib import Path
        from app.services import task_lease_service as lease_mod

        store = lease_mod.TaskLeaseStore(Path(":memory:"))
        monkeypatch.setattr(lease_mod, "_STORE", store)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        async def fake_get(api_base: str, path: str):
            return {
                "items": [
                    {"artifact_key": "task:alpha:task-1", "task_id": "task-1", "title": "Busy task", "status": "open"},
                    {"artifact_key": "task:alpha:task-2", "task_id": "task-2", "title": "Free task", "status": "open"},
                ]
            }

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        try:
            claim = store.claim(project="alpha", task_id="task-1", owner_agent="claude", session_id="sess-claude")
            result = await mcp_sse._execute_tool(
                "list_open_tasks",
                {"project": "alpha", "claim_filter": "claimed"},
                "http://test",
            )

            assert "Claimed open tasks: 1" in result
            assert "Busy task" in result
            assert "Free task" not in result
            assert "claimed_by=claude" in result
            assert f"lease_id={claim.lease.lease_id}" in result
        finally:
            store.close()

    async def test_list_open_tasks_independent_filter_hides_unknown_dependency_tasks(self, monkeypatch):
        async def fake_get(api_base: str, path: str):
            return {
                "items": [
                    {"artifact_key": "task:alpha:task-1", "task_id": "task-1", "title": "Unknown task", "status": "open"},
                    {
                        "artifact_key": "task:alpha:task-2",
                        "task_id": "task-2",
                        "title": "Explicitly independent task",
                        "status": "open",
                        "assignment_safety": "independent",
                    },
                ]
            }

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "list_open_tasks",
            {"project": "alpha", "assignment_filter": "independent"},
            "http://test",
        )

        assert "Explicitly independent task" in result
        assert "Unknown task" not in result
        assert "Assignment policy: Only tasks with explicit independence evidence" in result

    async def test_unknown_list_active_tasks_recovers_via_learned_project_work_pattern(self, monkeypatch):
        seen: dict[str, object] = {}

        async def fake_build_project_work_payload(api_base: str, args: dict[str, object], *, session_id: str | None = None):
            seen["api_base"] = api_base
            seen["args"] = args
            return {"facade": "project_work", "selected_route": {"tool": "list_open_tasks"}}

        monkeypatch.setattr(
            mcp_sse,
            "_learned_route_match",
            lambda **kwargs: {
                "tool": "project_work",
                "reason": "Matched a learned route pattern.",
                "metadata": {"intent": "List open project tasks and current priority work."},
            },
        )
        monkeypatch.setattr(mcp_sse, "_build_project_work_payload", fake_build_project_work_payload)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool("list_active_tasks", {"project": "alpha", "limit": 3}, "http://test")

        assert seen["api_base"] == "http://test"
        assert seen["args"] == {
            "project": "alpha",
            "intent": "List open project tasks and current priority work.",
            "allow_mutation": False,
            "detail": "full",
            "response_format": "json",
            "limit": 3,
        }
        assert '"facade": "project_work"' in result
        assert '"tool": "list_open_tasks"' in result

    async def test_unknown_current_priority_tasks_recovers_via_expert_and_records_pattern(self, monkeypatch):
        seen: dict[str, object] = {}

        async def fake_build_project_work_payload(api_base: str, args: dict[str, object], *, session_id: str | None = None):
            seen["args"] = args
            return {"facade": "project_work", "selected_route": {"tool": "list_open_tasks"}}

        async def fake_unknown_tool_llm_recovery(name: str, args: dict[str, object]):
            seen["recovered_name"] = name
            return {
                "tool": "project_work",
                "confidence": 0.91,
                "intent": "List open project tasks and current priority work.",
                "reason": "The unknown tool name refers to priority task listing.",
            }

        recorded: dict[str, object] = {}

        class FakePatternStore:
            def record(self, **kwargs):
                recorded.update(kwargs)
                return "pattern-1"

        monkeypatch.setattr(mcp_sse, "_learned_route_match", lambda **kwargs: None)
        monkeypatch.setattr(mcp_sse, "_unknown_tool_llm_recovery", fake_unknown_tool_llm_recovery)
        monkeypatch.setattr(mcp_sse, "get_route_pattern_store", lambda: FakePatternStore())
        monkeypatch.setattr(mcp_sse, "_build_project_work_payload", fake_build_project_work_payload)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool("current_priority_tasks", {"project": "alpha"}, "http://test")

        assert seen["recovered_name"] == "current_priority_tasks"
        assert seen["args"] == {
            "project": "alpha",
            "intent": "List open project tasks and current priority work.",
            "allow_mutation": False,
            "detail": "full",
            "response_format": "json",
            "limit": 50,
        }
        assert recorded["facade"] == "unknown_tool_recovery"
        assert recorded["intent_type"] == "project_work"
        assert recorded["tool"] == "project_work"
        assert recorded["metadata"]["intent"] == "List open project tasks and current priority work."
        assert '"facade": "project_work"' in result

    async def test_unknown_tool_error_guides_to_expert_entrypoints(self, monkeypatch):
        monkeypatch.setattr(mcp_sse, "_learned_route_match", lambda **kwargs: None)

        async def fake_unknown_tool_llm_recovery(name: str, args: dict[str, object]):
            return {}

        monkeypatch.setattr(mcp_sse, "_unknown_tool_llm_recovery", fake_unknown_tool_llm_recovery)
        response = await mcp_sse._handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "banana_wrench", "arguments": {"project": "alpha"}},
            },
            "http://test",
        )

        assert response["result"]["isError"] is True
        text = response["result"]["content"][0]["text"]
        assert "Unknown tool: banana_wrench." in text
        assert "start with mailbox_state for the current public workflow packet" in text
        assert "use mailbox_submit or mailbox_get for the public mailroom protocol" in text
        assert "use ask_project/project_work only when the mailbox packet directs a facade fallback" in text

    async def test_list_artifacts_tool_url_encodes_datetime_filters(self, monkeypatch):
        seen: dict[str, str] = {}

        async def fake_get(api_base: str, path: str):
            seen["path"] = path
            return {"items": []}

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        await mcp_sse._execute_tool(
            "list_artifacts",
            {
                "project": "alpha",
                "status": "open",
                "type": "task",
                "updated_after": "2026-04-17T00:00:00+04:00",
                "updated_before": "2026-04-18T00:00:00+04:00",
            },
            "http://test",
        )

        assert "updated_after=2026-04-17T00%3A00%3A00%2B04%3A00" in seen["path"]
        assert "updated_before=2026-04-18T00%3A00%3A00%2B04%3A00" in seen["path"]

    async def test_reconcile_completed_checkpoints_tool_posts_report_only_by_default(self, monkeypatch):
        posted: list[tuple[str, dict]] = []

        async def fake_post(api_base: str, path: str, payload: dict):
            posted.append((path, payload))
            return {
                "project": payload["project"],
                "scanned_tasks": 1,
                "candidates": [],
                "closed_artifact_keys": [],
                "skipped_artifact_keys": [],
                "errors": [],
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "reconcile_completed_checkpoints",
            {"project": "alpha"},
            "http://test",
        )

        assert posted == [
            (
                "/artifacts/reconcile-completed-checkpoints",
                {
                    "project": "alpha",
                    "close": False,
                    "close_policy": "strict",
                    "acted_by": "codex",
                    "action_source": "mcp_reconcile_completed_checkpoints",
                    "reason": "Completed checkpoint reconciliation requested through MCP.",
                    "limit": 100,
                },
            )
        ]
        assert '"closed_artifact_keys": []' in result
        assert '"stage": "testing"' in result
        assert '"feedback_expected": true' in result

    async def test_review_completed_checkpoint_scope_tool_posts_scope_review(self, monkeypatch):
        posted: list[tuple[str, dict]] = []

        async def fake_post(api_base: str, path: str, payload: dict):
            posted.append((path, payload))
            return {
                "project": payload["project"],
                "task_id": payload["task_id"],
                "checkpoint_change_id": payload["checkpoint_change_id"],
                "next_step_scope": payload["next_step_scope"],
                "saved_change_id": "change-scope-1",
                "content": "[task_checkpoint_scope_review]",
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "review_completed_checkpoint_scope",
            {
                "project": "alpha",
                "task_id": "task-1",
                "checkpoint_change_id": "checkpoint-1",
                "next_step_scope": "follow_up_task",
                "reason": "Separate follow-up slice.",
                "acted_by": "codex",
            },
            "http://test",
        )

        assert posted == [
            (
                "/artifacts/completed-checkpoint-scope-review",
                {
                    "project": "alpha",
                    "task_id": "task-1",
                    "checkpoint_change_id": "checkpoint-1",
                    "next_step_scope": "follow_up_task",
                    "reason": "Separate follow-up slice.",
                    "acted_by": "codex",
                    "source": "mcp_checkpoint_scope_review",
                },
            )
        ]
        assert '"next_step_scope": "follow_up_task"' in result
        assert '"stage": "testing"' in result

    async def test_review_completed_checkpoint_scopes_tool_posts_batch_scope_reviews(self, monkeypatch):
        posted: list[tuple[str, dict]] = []

        async def fake_post(api_base: str, path: str, payload: dict):
            posted.append((path, payload))
            return {
                "project": payload["project"],
                "saved_count": 2,
                "skipped_count": 0,
                "error_count": 0,
                "saved": [],
                "skipped": [],
                "errors": [],
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "review_completed_checkpoint_scopes",
            {
                "project": "alpha",
                "decisions": [
                    {
                        "task_id": "task-1",
                        "checkpoint_change_id": "checkpoint-1",
                        "next_step_scope": "follow_up_task",
                        "reason": "Separate follow-up slice.",
                    },
                    {
                        "task_id": "task-2",
                        "checkpoint_change_id": "checkpoint-2",
                        "next_step_scope": "same_artifact_remaining_work",
                    },
                ],
                "acted_by": "codex",
            },
            "http://test",
        )

        assert posted[0][0] == "/artifacts/completed-checkpoint-scope-review/batch"
        assert posted[0][1]["project"] == "alpha"
        assert posted[0][1]["decisions"][0]["next_step_scope"] == "follow_up_task"
        assert posted[0][1]["decisions"][1]["next_step_scope"] == "same_artifact_remaining_work"
        assert '"saved_count": 2' in result
        assert '"stage": "testing"' in result

    async def test_normalize_mcp_intent_returns_canonical_form_for_resume_requests(self, monkeypatch):
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())
        monkeypatch.setattr(mcp_sse, "_learned_route_match", lambda **kwargs: None)

        async def fake_llm(intent: str, *, project_id: str = "", top_n: int = 3):
            return {}

        monkeypatch.setattr(mcp_sse, "_normalize_mcp_intent_llm", fake_llm)

        result = await mcp_sse._execute_tool(
            "normalize_mcp_intent",
            {"intent": "Resume task 84c4e534-d722-4132-8660-4a56ed93f44a from checkpoint", "project_id": "mnemoforge", "top_n": 3},
            "http://test",
        )

        assert '"resolved_tool": "pull_task_context"' in result
        assert '"submit_to": "pull_task_context"' in result
        assert '"cache"' in result
        assert '"canonical_surface"' in result
        assert '"normalize_mcp_intent"' in result
        assert '"ready_to_execute": true' in result

    async def test_normalize_mcp_intent_routes_explicit_reactivation_to_reopen_task(self, monkeypatch):
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())
        monkeypatch.setattr(mcp_sse, "_learned_route_match", lambda **kwargs: None)

        async def fake_llm(intent: str, *, project_id: str = "", top_n: int = 3):
            return {}

        monkeypatch.setattr(mcp_sse, "_normalize_mcp_intent_llm", fake_llm)

        result = await mcp_sse._execute_tool(
            "normalize_mcp_intent",
            {"intent": "Reopen task 84c4e534-d722-4132-8660-4a56ed93f44a", "project_id": "mnemoforge", "top_n": 3},
            "http://test",
        )

        assert '"resolved_tool": "reopen_task"' in result
        assert '"submit_to": "reopen_task"' in result

    async def test_normalize_mcp_intent_uses_learned_route_pattern(self, monkeypatch):
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())
        monkeypatch.setattr(
            mcp_sse,
            "_learned_route_match",
            lambda **kwargs: {
                "tool": "list_open_tasks",
                "reason": "Matched a learned route pattern.",
                "confidence": 0.91,
                "backend_used": "learned_semantic",
                "pattern_id": "pat-open-1",
                "score": 0.9,
                "matched_by": "semantic",
            },
        )

        result = await mcp_sse._execute_tool(
            "normalize_mcp_intent",
            {"intent": "show me the queue", "project_id": "mnemoforge", "top_n": 3},
            "http://test",
        )

        assert '"resolved_tool": "list_open_tasks"' in result
        assert '"matched_pattern_id": "pat-open-1"' in result

    async def test_normalize_mcp_intent_llm_records_learned_pattern(self, monkeypatch):
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())
        monkeypatch.setattr(mcp_sse, "_learned_route_match", lambda **kwargs: None)

        async def fake_llm(intent: str, *, project_id: str = "", top_n: int = 3):
            return {"tool": "list_open_tasks", "confidence": 0.89, "reason": "This asks for open work."}

        monkeypatch.setattr(mcp_sse, "_normalize_mcp_intent_llm", fake_llm)
        monkeypatch.setattr(mcp_sse, "_record_learned_route_pattern", lambda **kwargs: "learned-norm-1")

        result = await mcp_sse._execute_tool(
            "normalize_mcp_intent",
            {"intent": "what should I do next", "project_id": "mnemoforge", "top_n": 3},
            "http://test",
        )

        assert '"resolved_tool": "list_open_tasks"' in result
        assert '"learned_pattern_id": "learned-norm-1"' in result

    async def test_list_tool_families_returns_compact_catalog(self, monkeypatch):
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool("list_tool_families", {}, "http://test")

        assert '"tool_discovery"' in result
        assert '"project_knowledge"' in result

    async def test_tool_family_tools_returns_full_tool_schema(self, monkeypatch):
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "tool_family_tools",
            {"family": "tool_discovery", "depth": "full", "limit": 20},
            "http://test",
        )

        assert '"family": "tool_discovery"' in result
        assert '"tool_feedback"' in result
        assert '"inputSchema"' in result

    async def test_report_task_checkpoint_records_task_change_and_session_context(self, monkeypatch):
        posted: list[tuple[str, dict]] = []
        lease_store, work_token = _install_task_claim_store(monkeypatch)

        async def fake_post(api_base: str, path: str, payload: dict):
            posted.append((path, payload))
            return {"id": "change-1", "task_id": "task-1", "stage": "planning", "status": "planning"}

        class _FakeSessionStore:
            def __init__(self):
                self.ctx: dict[str, dict] = {}

            async def patch_context(self, session_id: str, patch: dict):
                ctx = self.ctx.setdefault(session_id, {})
                for key, value in patch.items():
                    ctx[key] = value

        fake_store = _FakeSessionStore()
        from app.services import mcp_session_store

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())
        monkeypatch.setattr(mcp_session_store, "get_session_store", lambda: fake_store)

        try:
            result = await mcp_sse._execute_tool(
                "report_task_checkpoint",
                {
                    "project": "alpha",
                    "task_id": "task-1",
                    "stage": "planning",
                    "summary": "Framed the task and confirmed the first slice.",
                    "checkpoint_mode": "lightweight",
                    "blockers": ["Waiting for schema clarification."],
                    "stage_evidence_refs": ["checkpoint:framing-source"],
                    "next_step": "Record decision candidates.",
                    "reason": "Initial planning checkpoint.",
                    "acted_by": "codex",
                    "source": "mcp",
                    "session_id": "sess-1",
                    "work_token": work_token,
                },
                "http://test",
                session_id="sess-1",
            )
        finally:
            lease_store.close()

        change_post = next(item for item in posted if item[0] == "/project/tasks/task-1/changes")
        assert "[task_checkpoint]" in change_post[1]["content"]
        assert "Stage evidence refs: checkpoint:framing-source" in change_post[1]["content"]
        assert "task_checkpoint" in change_post[1]["tags"]
        assert "checkpoint_mode:lightweight" in change_post[1]["tags"]
        assert fake_store.ctx["sess-1"]["task_checkpoint_recorded"] is True
        assert fake_store.ctx["sess-1"]["current_task_checkpoint"]["stage"] == "planning"
        assert fake_store.ctx["sess-1"]["stage_evidence"] == "checkpoint:change-1"
        assert "Checkpoint recorded for task task-1" in result
        assert "stage_evidence=checkpoint:change-1" in result

    async def test_record_task_checkpoint_creates_resume_handoff_for_blocked_stage(self, monkeypatch):
        posted: list[tuple[str, dict]] = []
        lease_store, work_token = _install_task_claim_store(monkeypatch)

        async def fake_post(api_base: str, path: str, payload: dict):
            posted.append((path, payload))
            if path == "/models/handoff":
                return {"memory_id": "handoff-1", "handoff_label": payload["handoff_label"], "task_id": payload["task_id"]}
            return {"id": "change-1", "task_id": "task-1", "stage": "blocked", "status": "paused"}

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        try:
            result = await mcp_sse._execute_tool(
                "record_task_checkpoint",
                {
                    "project": "alpha",
                    "task_id": "task-1",
                    "stage": "blocked",
                    "summary": "Implementation is paused at the checkpoint facade.",
                    "blockers": ["Need API shape confirmation."],
                    "decisions": ["Store all checkpoints as task_change."],
                    "changed_files": ["app/routers/mcp_sse.py"],
                    "verification": ["Focused MCP test will cover blocked checkpoint."],
                    "remaining_risk": ["Handoff packet creation is best effort."],
                    "next_step": "Resume from the MCP handler.",
                    "acted_by": "codex",
                    "to_agent": "codex",
                    "session_id": "sess-1",
                    "work_token": work_token,
                },
                "http://test",
            )
        finally:
            lease_store.close()

        change_post = next(item for item in posted if item[0] == "/project/tasks/task-1/changes")
        handoff_post = next(item for item in posted if item[0] == "/models/handoff")
        assert "Decisions: Store all checkpoints as task_change." in change_post[1]["content"]
        assert "Changed files: app/routers/mcp_sse.py" in change_post[1]["content"]
        assert handoff_post[1]["phase"] == "blocked"
        assert handoff_post[1]["write_scope"] == ["app/routers/mcp_sse.py"]
        assert "handoff_packet_created" in result
        assert "handoff-1" in result

    async def test_record_task_checkpoint_blocks_obvious_scope_mismatch(self, monkeypatch):
        posted: list[tuple[str, dict]] = []
        lease_store, work_token = _install_task_claim_store(monkeypatch, task_id="reconstruct-projects")

        async def fake_get(api_base: str, path: str):
            assert path == "/project/tasks/reconstruct-projects?project=alpha"
            return {
                "task_id": "reconstruct-projects",
                "title": "Reconstruct any project from governed memory",
                "description": "Build source-loss reconstruction bundles from project tasks, laws, decisions, and component contracts.",
                "tags": ["source-loss-reconstruction", "project-genome"],
            }

        async def fake_post(api_base: str, path: str, payload: dict):
            posted.append((path, payload))
            return {"id": "unexpected"}

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        try:
            result = await mcp_sse._execute_tool(
                "record_task_checkpoint",
                {
                    "project": "alpha",
                    "task_id": "reconstruct-projects",
                    "stage": "in_progress",
                    "summary": "Added public usage conditions and Docker Hub publish readiness checks.",
                    "decisions": ["Treat public usage conditions as a release artifact."],
                    "changed_files": ["docs/USAGE_CONDITIONS.md", "scripts/publish_docker_image.py"],
                    "verification": ["Public release readiness tests passed."],
                    "next_step": "Prepare a public release checklist.",
                    "acted_by": "codex",
                    "session_id": "sess-1",
                    "work_token": work_token,
                },
                "http://test",
            )
        finally:
            lease_store.close()
        data = json.loads(result)

        assert posted == []
        assert data["error"] == "checkpoint_scope_mismatch"
        assert data["task_checkpoint_recorded"] is False
        assert data["recommended_next_tools"] == ["list_open_tasks", "list_artifacts", "reopen_task"]

    async def test_record_task_checkpoint_allows_scope_confirmation_override(self, monkeypatch):
        posted: list[tuple[str, dict]] = []
        lease_store, work_token = _install_task_claim_store(monkeypatch, task_id="reconstruct-projects")

        async def fake_get(api_base: str, path: str):
            return {
                "task_id": "reconstruct-projects",
                "title": "Reconstruct any project from governed memory",
                "description": "Build source-loss reconstruction bundles from project tasks, laws, decisions, and component contracts.",
                "tags": ["source-loss-reconstruction"],
            }

        async def fake_post(api_base: str, path: str, payload: dict):
            posted.append((path, payload))
            return {"id": "change-1", "task_id": "reconstruct-projects", "stage": "in_progress", "status": "active"}

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        try:
            result = await mcp_sse._execute_tool(
                "record_task_checkpoint",
                {
                    "project": "alpha",
                    "task_id": "reconstruct-projects",
                    "stage": "in_progress",
                    "summary": "Added public usage conditions and Docker Hub publish readiness checks.",
                    "scope_confirmation": "current checkpoint belongs to this task",
                    "acted_by": "codex",
                    "session_id": "sess-1",
                    "work_token": work_token,
                },
                "http://test",
            )
        finally:
            lease_store.close()

        assert posted[0][0] == "/project/tasks/reconstruct-projects/changes"
        assert "Checkpoint recorded for task reconstruct-projects" in result

    async def test_draft_task_checkpoint_returns_record_tool_args_without_writing(self, monkeypatch):
        posted: list[tuple[str, dict]] = []

        async def fake_post(api_base: str, path: str, payload: dict):
            posted.append((path, payload))
            return {"id": "unexpected"}

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "draft_task_checkpoint",
            {
                "project": "alpha",
                "task_id": "task-1",
                "stage": "handoff",
                "status": "paused",
                "use_llm": False,
                "raw_notes": "\n".join(
                    [
                        "Summary: Drafted checkpoint args for review.",
                        "Changed files: app/services/memory_scribe_service.py",
                        "Verification: pytest tests/test_memory_scribe_service.py passed",
                        "Next step: Submit record_task_checkpoint after review.",
                    ]
                ),
            },
            "http://test",
        )

        data = json.loads(result)
        assert posted == []
        assert data["mutates_memory"] is False
        assert data["recommended_next_tool"] == "record_task_checkpoint"
        assert data["record_task_checkpoint_args"]["task_id"] == "task-1"
        assert data["record_task_checkpoint_args"]["source"] == "memory_scribe"

    async def test_stenographer_mcp_tools_enforce_active_work(self, monkeypatch):
        from pathlib import Path
        from app.services import task_lease_service as lease_mod
        from app.services import stenographer_service as stenographer_mod

        store = stenographer_mod.StenographerStore(Path(":memory:"))
        lease_store = lease_mod.TaskLeaseStore(Path(":memory:"))
        monkeypatch.setattr(stenographer_mod, "_STORE", store)
        monkeypatch.setattr(lease_mod, "_STORE", lease_store)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())
        try:
            denied = await mcp_sse._execute_tool(
                "record_stenographer_span",
                {
                    "project": "alpha",
                    "task_id": "task-1",
                    "agent_id": "codex",
                    "session_id": "sess-1",
                    "kind": "verification",
                    "content": "13 passed",
                },
                "http://test",
            )
            denied_data = json.loads(denied)
            assert denied_data["error"] == "work_session_required"
            assert denied_data["required_next_tool"] == "start_work_session"

            claimed = await mcp_sse._execute_tool(
                "claim_task",
                {
                    "project": "alpha",
                    "task_id": "task-1",
                    "owner_agent": "codex",
                    "session_id": "sess-1",
                },
                "http://test",
            )
            claimed_data = json.loads(claimed)
            assert claimed_data["status"] in {"claimed", "renewed"}
            work_token = claimed_data.get("work_token", "")

            started = await mcp_sse._execute_tool(
                "start_work_session",
                {
                    "project": "alpha",
                    "task_id": "task-1",
                    "agent_id": "codex",
                    "session_id": "sess-1",
                    "work_id": "work-1",
                    "work_token": work_token,
                },
                "http://test",
            )
            assert json.loads(started)["status"] == "active"

            recorded = await mcp_sse._execute_tool(
                "record_stenographer_span",
                {
                    "project": "alpha",
                    "task_id": "task-1",
                    "agent_id": "codex",
                    "session_id": "sess-1",
                    "work_id": "work-1",
                    "kind": "verification",
                    "source": "pytest",
                    "content": "13 passed",
                },
                "http://test",
            )
            recorded_data = json.loads(recorded)
            assert recorded_data["work_id"] == "work-1"
            assert recorded_data["excluded_from_learning"] is True
        finally:
            store.close()
            lease_store.close()

    async def test_task_claim_mcp_tools_block_second_agent_until_timeout(self, monkeypatch):
        from pathlib import Path
        from app.services import task_lease_service as lease_mod

        store = lease_mod.TaskLeaseStore(Path(":memory:"))
        monkeypatch.setattr(lease_mod, "_STORE", store)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())
        try:
            claimed = await mcp_sse._execute_tool(
                "claim_task",
                {
                    "project": "alpha",
                    "task_id": "task-1",
                    "owner_agent": "codex",
                    "session_id": "sess-codex",
                    "lease_ttl_seconds": 30,
                },
                "http://test",
            )
            claim_data = json.loads(claimed)
            lease_id = claim_data["lease"]["lease_id"]
            assert claim_data["status"] == "claimed"
            assert claim_data["lease"]["status"] == "active"

            conflict = await mcp_sse._execute_tool(
                "claim_task",
                {
                    "project": "alpha",
                    "task_id": "task-1",
                    "owner_agent": "claude",
                    "session_id": "sess-claude",
                },
                "http://test",
            )
            conflict_data = json.loads(conflict)
            assert conflict_data["status"] == "conflict"
            assert conflict_data["claim_allowed"] is False
            assert conflict_data["error"]["active_lease"]["lease_id"] == lease_id

            released = await mcp_sse._execute_tool(
                "release_task_claim",
                {
                    "lease_id": lease_id,
                    "owner_agent": "codex",
                    "session_id": "sess-codex",
                    "reason": "completed",
                },
                "http://test",
            )
            assert json.loads(released)["lease"]["status"] == "released"
        finally:
            store.close()

    async def test_task_claim_mcp_tools_heartbeat_and_list(self, monkeypatch):
        from pathlib import Path
        from app.services import task_lease_service as lease_mod

        store = lease_mod.TaskLeaseStore(Path(":memory:"))
        monkeypatch.setattr(lease_mod, "_STORE", store)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())
        try:
            claimed = await mcp_sse._execute_tool(
                "claim_task",
                {
                    "project": "alpha",
                    "task_id": "task-1",
                    "agent_id": "codex",
                    "session_id": "sess-codex",
                    "lease_ttl_seconds": 30,
                },
                "http://test",
            )
            lease_id = json.loads(claimed)["lease"]["lease_id"]

            heartbeat = await mcp_sse._execute_tool(
                "heartbeat_task_claim",
                {
                    "lease_id": lease_id,
                    "agent_id": "codex",
                    "session_id": "sess-codex",
                    "lease_ttl_seconds": 60,
                },
                "http://test",
            )
            assert json.loads(heartbeat)["status"] == "renewed"

            listed = await mcp_sse._execute_tool(
                "list_task_claims",
                {"project": "alpha", "task_id": "task-1", "status": "active"},
                "http://test",
            )
            listed_data = json.loads(listed)
            assert listed_data["count"] == 1
            assert listed_data["leases"][0]["lease_id"] == lease_id
        finally:
            store.close()

    async def test_heartbeat_task_claim_returns_structured_expired_conflict(self, monkeypatch):
        from datetime import datetime, timedelta, timezone
        from pathlib import Path
        from app.services import task_lease_service as lease_mod

        store = lease_mod.TaskLeaseStore(Path(":memory:"))
        monkeypatch.setattr(lease_mod, "_STORE", store)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())
        try:
            old_now = datetime.now(timezone.utc) - timedelta(seconds=30)
            claim = store.claim(
                project="alpha",
                task_id="task-1",
                owner_agent="codex",
                session_id="sess-codex",
                lease_ttl_seconds=5,
                now=old_now,
            )
            heartbeat = await mcp_sse._execute_tool(
                "heartbeat_task_claim",
                {
                    "lease_id": claim.lease.lease_id,
                    "agent_id": "codex",
                    "session_id": "sess-codex",
                },
                "http://test",
            )
            data = json.loads(heartbeat)
            assert data["status"] == "conflict"
            assert data["error"]["error"] == "lease_expired"
            assert data["lease"]["status"] == "expired"
            assert "claim_task again" in data["next_safe_action"]
        finally:
            store.close()

    async def test_start_task_session_starts_claim_work_and_checkpoint(self, monkeypatch):
        from pathlib import Path
        from app.services import task_lease_service as lease_mod
        from app.services import stenographer_service as stenographer_mod

        lease_store = lease_mod.TaskLeaseStore(Path(":memory:"))
        stenographer_store = stenographer_mod.StenographerStore(Path(":memory:"))
        monkeypatch.setattr(lease_mod, "_STORE", lease_store)
        monkeypatch.setattr(stenographer_mod, "_STORE", stenographer_store)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())
        async def fake_post(api_base: str, path: str, payload: dict):
            if path.startswith("/project/tasks/") and path.endswith("/changes"):
                return {"id": "checkpoint-1", **payload}
            raise AssertionError(path)
        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        try:
            result = await mcp_sse._execute_tool(
                "start_task_session",
                {
                    "project": "alpha",
                    "task_id": "task-1",
                    "agent_id": "codex",
                    "session_id": "sess-codex",
                    "summary": "Starting task execution",
                },
                "http://test",
            )
            data = json.loads(result)
            assert data["status"] == "started"
            assert data["owner_session_id"] == "sess-codex"
            assert data["lease"]["status"] == "active"
            assert data["auto_heartbeat"]["enabled"] is True
            assert data["work_session"]["status"] == "active"
            assert data["checkpoint"]["id"]
        finally:
            lease_store.close()
            stenographer_store.close()

    async def test_finish_task_session_stops_start_session_auto_heartbeat(self, monkeypatch):
        from pathlib import Path
        from app.services import task_lease_service as lease_mod
        from app.services import stenographer_service as stenographer_mod

        lease_store = lease_mod.TaskLeaseStore(Path(":memory:"))
        stenographer_store = stenographer_mod.StenographerStore(Path(":memory:"))
        monkeypatch.setattr(lease_mod, "_STORE", lease_store)
        monkeypatch.setattr(stenographer_mod, "_STORE", stenographer_store)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        async def fake_post(api_base: str, path: str, payload: dict):
            if path.startswith("/project/tasks/") and path.endswith("/changes"):
                return {"id": f"checkpoint-{len(path)}", **payload}
            raise AssertionError(path)

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        try:
            started = await mcp_sse._execute_tool(
                "start_task_session",
                {
                    "project": "alpha",
                    "task_id": "task-1",
                    "agent_id": "codex",
                    "session_id": "sess-codex",
                    "lease_ttl_seconds": 30,
                    "heartbeat_seconds": 1,
                    "summary": "Starting task execution",
                },
                "http://test",
            )
            start_data = json.loads(started)
            lease_id = start_data["lease"]["lease_id"]
            assert lease_id in lease_mod._HEARTBEATS

            finished = await mcp_sse._execute_tool(
                "finish_task_session",
                {
                    "project": "alpha",
                    "task_id": "task-1",
                    "work_id": start_data["work_session"]["work_id"],
                    "agent_id": "codex",
                    "session_id": "sess-codex",
                    "summary": "Completed implementation",
                    "verification": ["verification_contour:alpha:focused-tests passed"],
                    "changed_files": ["app/routers/mcp_sse.py"],
                    "next_step": "none",
                },
                "http://test",
            )
            finish_data = json.loads(finished)
            assert finish_data["status"] == "finished"
            assert lease_id not in lease_mod._HEARTBEATS
        finally:
            lease_store.close()
            stenographer_store.close()

    async def test_start_task_session_conflict_returns_owner_session(self, monkeypatch):
        from pathlib import Path
        from app.services import task_lease_service as lease_mod

        store = lease_mod.TaskLeaseStore(Path(":memory:"))
        monkeypatch.setattr(lease_mod, "_STORE", store)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())
        try:
            store.claim(
                project="alpha",
                task_id="task-1",
                owner_agent="claude",
                session_id="sess-claude",
                lease_ttl_seconds=900,
            )
            result = await mcp_sse._execute_tool(
                "start_task_session",
                {
                    "project": "alpha",
                    "task_id": "task-1",
                    "agent_id": "codex",
                    "session_id": "sess-codex",
                },
                "http://test",
            )
            data = json.loads(result)
            assert data["status"] == "conflict"
            assert data["owner_agent"] == "claude"
            assert data["owner_session_id"] == "sess-claude"
        finally:
            store.close()

    async def test_start_task_session_resumes_same_task_work_session(self, monkeypatch):
        from pathlib import Path
        from app.services import task_lease_service as lease_mod
        from app.services import stenographer_service as stenographer_mod

        lease_store = lease_mod.TaskLeaseStore(Path(":memory:"))
        stenographer_store = stenographer_mod.StenographerStore(Path(":memory:"))
        monkeypatch.setattr(lease_mod, "_STORE", lease_store)
        monkeypatch.setattr(stenographer_mod, "_STORE", stenographer_store)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        async def fake_post(api_base: str, path: str, payload: dict):
            if path.startswith("/project/tasks/") and path.endswith("/changes"):
                return {"id": "checkpoint-resume-1", **payload}
            raise AssertionError(path)

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        try:
            first = json.loads(
                await mcp_sse._execute_tool(
                    "start_task_session",
                    {
                        "project": "alpha",
                        "task_id": "task-1",
                        "agent_id": "codex",
                        "session_id": "sess-codex",
                        "auto_heartbeat": False,
                    },
                    "http://test",
                )
            )
            resumed = json.loads(
                await mcp_sse._execute_tool(
                    "start_task_session",
                    {
                        "project": "alpha",
                        "task_id": "task-1",
                        "agent_id": "codex",
                        "session_id": "sess-codex",
                        "auto_heartbeat": False,
                    },
                    "http://test",
                )
            )

            assert resumed["status"] == "started"
            assert resumed["lease_status"] == "renewed"
            assert resumed["work_session_resumed"] is True
            assert resumed["work_session"]["work_id"] == first["work_session"]["work_id"]
        finally:
            lease_store.close()
            stenographer_store.close()

    async def test_finish_task_session_completes_and_releases_claim(self, monkeypatch):
        from pathlib import Path
        from app.services import task_lease_service as lease_mod
        from app.services import stenographer_service as stenographer_mod

        lease_store = lease_mod.TaskLeaseStore(Path(":memory:"))
        stenographer_store = stenographer_mod.StenographerStore(Path(":memory:"))
        monkeypatch.setattr(lease_mod, "_STORE", lease_store)
        monkeypatch.setattr(stenographer_mod, "_STORE", stenographer_store)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        async def fake_post(api_base: str, path: str, payload: dict):
            if path.startswith("/project/tasks/") and path.endswith("/changes"):
                return {"id": "checkpoint-finish-1", **payload}
            raise AssertionError(path)

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        try:
            lease_store.claim(project="alpha", task_id="task-1", owner_agent="codex", session_id="sess-codex")
            stenographer_store.start_work_session(
                project="alpha",
                task_id="task-1",
                agent_id="codex",
                session_id="sess-codex",
                work_id="work-1",
            )
            result = await mcp_sse._execute_tool(
                "finish_task_session",
                {
                    "project": "alpha",
                    "task_id": "task-1",
                    "work_id": "work-1",
                    "agent_id": "codex",
                    "session_id": "sess-codex",
                    "summary": "Completed implementation",
                    "verification": ["pytest -q passed"],
                    "changed_files": ["app/routers/mcp_sse.py"],
                    "next_step": "none",
                },
                "http://test",
            )
            data = json.loads(result)
            assert data["status"] == "finished"
            assert data["work_session"]["status"] == "completed"
            assert data["release"]["status"] == "released"
        finally:
            lease_store.close()
            stenographer_store.close()

    async def test_finish_task_session_auto_detects_work_id(self, monkeypatch):
        """finish_task_session should auto-detect work_id from active work session."""
        from pathlib import Path
        from app.services import task_lease_service as lease_mod
        from app.services import stenographer_service as stenographer_mod

        lease_store = lease_mod.TaskLeaseStore(Path(":memory:"))
        stenographer_store = stenographer_mod.StenographerStore(Path(":memory:"))
        monkeypatch.setattr(lease_mod, "_STORE", lease_store)
        monkeypatch.setattr(stenographer_mod, "_STORE", stenographer_store)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        async def fake_post(api_base: str, path: str, payload: dict):
            if path.startswith("/project/tasks/") and path.endswith("/changes"):
                return {"id": "checkpoint-auto-work", **payload}
            raise AssertionError(path)

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        try:
            lease_store.claim(project="alpha", task_id="task-1", owner_agent="codex", session_id="sess-codex")
            stenographer_store.start_work_session(
                project="alpha",
                task_id="task-1",
                agent_id="codex",
                session_id="sess-codex",
                work_id="auto-work-123",
            )
            result = await mcp_sse._execute_tool(
                "finish_task_session",
                {
                    "project": "alpha",
                    "task_id": "task-1",
                    "agent_id": "codex",
                    "session_id": "sess-codex",
                    "summary": "Auto-detected work_id",
                    "verification": ["auto-detection works"],
                    "changed_files": ["app/routers/mcp_sse.py"],
                    "next_step": "none",
                },
                "http://test",
            )
            data = json.loads(result)
            assert data["status"] == "finished"
            assert data["work_session"]["work_id"] == "auto-work-123"
            assert data["work_session"]["status"] == "completed"
        finally:
            lease_store.close()
            stenographer_store.close()

    async def test_finish_task_session_returns_work_session_not_found_without_work_id(self, monkeypatch):
        """finish_task_session should error when no active work session exists and work_id not provided."""
        from pathlib import Path
        from app.services import task_lease_service as lease_mod
        from app.services import stenographer_service as stenographer_mod

        lease_store = lease_mod.TaskLeaseStore(Path(":memory:"))
        stenographer_store = stenographer_mod.StenographerStore(Path(":memory:"))
        monkeypatch.setattr(lease_mod, "_STORE", lease_store)
        monkeypatch.setattr(stenographer_mod, "_STORE", stenographer_store)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())
        monkeypatch.setattr(mcp_sse, "_post", AsyncMock())

        try:
            lease_store.claim(project="alpha", task_id="task-1", owner_agent="codex", session_id="sess-codex")
            # No work session started
            result = await mcp_sse._execute_tool(
                "finish_task_session",
                {
                    "project": "alpha",
                    "task_id": "task-1",
                    "agent_id": "codex",
                    "session_id": "sess-codex",
                },
                "http://test",
            )
            data = json.loads(result)
            assert data["status"] == "conflict"
            assert data["error"] == "work_session_not_found"
        finally:
            lease_store.close()
            stenographer_store.close()

    async def test_finish_task_session_returns_release_conflict(self, monkeypatch):
        from pathlib import Path
        from app.services import task_lease_service as lease_mod
        from app.services import stenographer_service as stenographer_mod

        lease_store = lease_mod.TaskLeaseStore(Path(":memory:"))
        stenographer_store = stenographer_mod.StenographerStore(Path(":memory:"))
        monkeypatch.setattr(lease_mod, "_STORE", lease_store)
        monkeypatch.setattr(stenographer_mod, "_STORE", stenographer_store)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())
        posted: list[tuple[str, dict]] = []

        async def fake_post(api_base: str, path: str, payload: dict):
            posted.append((path, payload))
            if path.startswith("/project/tasks/") and path.endswith("/changes"):
                return {"id": "checkpoint-finish-2", **payload}
            raise AssertionError(path)

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        try:
            lease_store.claim(project="alpha", task_id="task-1", owner_agent="claude", session_id="sess-claude")
            stenographer_store.start_work_session(
                project="alpha",
                task_id="task-1",
                agent_id="codex",
                session_id="sess-codex",
                work_id="work-1",
            )
            result = await mcp_sse._execute_tool(
                "finish_task_session",
                {
                    "project": "alpha",
                    "task_id": "task-1",
                    "work_id": "work-1",
                    "agent_id": "codex",
                    "session_id": "sess-codex",
                    "verification": ["pytest -q passed"],
                    "changed_files": ["app/routers/mcp_sse.py"],
                    "next_step": "none",
                },
                "http://test",
            )
            data = json.loads(result)
            assert data["status"] == "conflict"
            assert data["error"] == "lease_owner_mismatch"
            assert data["owner_agent"] == "claude"
            assert data["owner_session_id"] == "sess-claude"
            assert posted == []
            assert stenographer_store.get_state(agent_id="codex", session_id="sess-codex").state == "active_work"
        finally:
            lease_store.close()
            stenographer_store.close()

    async def test_start_task_session_rolls_back_when_checkpoint_fails(self, monkeypatch):
        from pathlib import Path
        from app.services import task_lease_service as lease_mod
        from app.services import stenographer_service as stenographer_mod

        lease_store = lease_mod.TaskLeaseStore(Path(":memory:"))
        stenographer_store = stenographer_mod.StenographerStore(Path(":memory:"))
        monkeypatch.setattr(lease_mod, "_STORE", lease_store)
        monkeypatch.setattr(stenographer_mod, "_STORE", stenographer_store)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        async def fail_post(api_base: str, path: str, payload: dict):
            raise RuntimeError("checkpoint write failed")

        monkeypatch.setattr(mcp_sse, "_post", fail_post)
        try:
            with pytest.raises(RuntimeError, match="checkpoint write failed"):
                await mcp_sse._execute_tool(
                    "start_task_session",
                    {
                        "project": "alpha",
                        "task_id": "task-1",
                        "agent_id": "codex",
                        "session_id": "sess-codex",
                    },
                    "http://test",
                )
            assert lease_store.get_active_claim(project="alpha", task_id="task-1") is None
            assert stenographer_store.get_state(agent_id="codex", session_id="sess-codex").state == "no_active_work"
        finally:
            lease_store.close()
            stenographer_store.close()

    async def test_task_claim_mcp_tools_require_session_id(self, monkeypatch):
        from pathlib import Path
        from app.services import task_lease_service as lease_mod

        store = lease_mod.TaskLeaseStore(Path(":memory:"))
        monkeypatch.setattr(lease_mod, "_STORE", store)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())
        try:
            with pytest.raises(ValueError, match="session_id is required for claim_task"):
                await mcp_sse._execute_tool(
                    "claim_task",
                    {
                        "project": "alpha",
                        "task_id": "task-1",
                        "owner_agent": "codex",
                    },
                    "http://test",
                )
        finally:
            store.close()

    async def test_task_claim_mcp_force_release(self, monkeypatch):
        from pathlib import Path
        from app.services import task_lease_service as lease_mod

        store = lease_mod.TaskLeaseStore(Path(":memory:"))
        monkeypatch.setattr(lease_mod, "_STORE", store)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())
        try:
            claimed = await mcp_sse._execute_tool(
                "claim_task",
                {
                    "project": "alpha",
                    "task_id": "task-1",
                    "owner_agent": "codex",
                    "session_id": "sess-codex",
                    "lease_ttl_seconds": 30,
                },
                "http://test",
            )
            lease_id = json.loads(claimed)["lease"]["lease_id"]

            released = await mcp_sse._execute_tool(
                "force_release_task_claim",
                {
                    "lease_id": lease_id,
                    "acted_by": "operator",
                    "reason": "manual override",
                },
                "http://test",
            )
            released_data = json.loads(released)
            assert released_data["force_released"] is True
            assert released_data["lease"]["status"] == "released"
            assert "force_release:operator:manual override" in released_data["lease"]["release_reason"]
        finally:
            store.close()

    async def test_checkpoint_draft_mcp_tools_create_preview_from_spans(self, monkeypatch):
        from pathlib import Path
        from uuid import uuid4
        from app.services import checkpoint_draft_service as draft_mod
        from app.services import stenographer_service as stenographer_mod

        root = Path("qdrant_data") / "test_mcp_checkpoint_drafts"
        root.mkdir(parents=True, exist_ok=True)
        stenographer_store = stenographer_mod.StenographerStore(root / f"stenographer-{uuid4().hex}.db")
        draft_store = draft_mod.CheckpointDraftStore(root / f"drafts-{uuid4().hex}.db")
        lease_store, work_token = _install_task_claim_store(monkeypatch)
        monkeypatch.setattr(draft_mod, "get_stenographer_store", lambda: stenographer_store)
        monkeypatch.setattr(draft_mod, "_STORE", draft_store)
        monkeypatch.setattr(stenographer_mod, "_STORE", stenographer_store)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())
        try:
            stenographer_store.start_work_session(
                project="alpha",
                task_id="task-1",
                agent_id="codex",
                session_id="sess-1",
                work_id="work-1",
            )
            for kind, content in (
                ("fact", "Implemented checkpoint draft approval by reference."),
                ("decision", "Approve by draft_id and version, not by replaying payload."),
                ("verification", "pytest tests/test_checkpoint_draft_service.py passed"),
                ("changed_files", "app/services/checkpoint_draft_service.py; tests/test_checkpoint_draft_service.py"),
                ("next_step", "Expose approve-by-reference through MCP."),
            ):
                stenographer_store.record_span(
                    project="alpha",
                    task_id="task-1",
                    agent_id="codex",
                    session_id="sess-1",
                    work_id="work-1",
                    kind=kind,
                    source="test",
                    content=content,
                )

            drafted = await mcp_sse._execute_tool(
                "draft_checkpoint_from_spans",
                {
                    "project": "alpha",
                    "task_id": "task-1",
                    "work_id": "work-1",
                    "agent_id": "codex",
                    "session_id": "sess-1",
                    "use_llm": False,
                    "preserve_evidence": True,
                },
                "http://test",
            )
            drafted_data = json.loads(drafted)
            assert drafted_data["version"] == 1
            assert drafted_data["mutates_memory"] is False
            assert drafted_data["recommended_next_tool"] == "approve_checkpoint_draft"
            assert drafted_data["record_task_checkpoint_args"]["changed_files"] == [
                "app/services/checkpoint_draft_service.py",
                "tests/test_checkpoint_draft_service.py",
            ]
            assert drafted_data["source_evidence"]["preserved"] is True
            assert drafted_data["source_evidence"]["items"]

            preview = await mcp_sse._execute_tool(
                "get_checkpoint_draft",
                {"draft_id": drafted_data["draft_id"], "view": "preview"},
                "http://test",
            )
            preview_data = json.loads(preview)
            assert "record_task_checkpoint_args" not in preview_data
            assert "source_evidence" not in preview_data
            assert preview_data["metrics"]["estimated_saved_chars"] > 0

            async def fake_save(payload: dict) -> dict:
                return {"id": "change-approved"}

            await draft_mod.approve_checkpoint_draft(
                drafted_data["draft_id"],
                drafted_data["version"],
                save_checkpoint=fake_save,
                store=draft_store,
            )

            approved_preview = await mcp_sse._execute_tool(
                "get_checkpoint_draft",
                {"draft_id": drafted_data["draft_id"], "view": "preview"},
                "http://test",
            )
            approved_preview_data = json.loads(approved_preview)
            assert approved_preview_data["status"] == "approved"
            assert approved_preview_data["recommended_next_tool"] == "get_task_execution_context"

            ended = await mcp_sse._execute_tool(
                "end_work_session",
                {
                    "project": "alpha",
                    "task_id": "task-1",
                    "work_id": "work-1",
                    "agent_id": "codex",
                    "session_id": "sess-1",
                    "status": "completed",
                    "result": "Approved clerk draft by reference.",
                    "work_token": work_token,
                },
                "http://test",
            )
            ended_data = json.loads(ended)
            assert ended_data.get("recommended_next_tool") == "get_task_execution_context", ended_data
            assert ended_data["approved_checkpoint_draft_id"] == drafted_data["draft_id"]
            assert ended_data["saved_change_id"] == "change-approved"
        finally:
            stenographer_store.close()
            draft_store.close()
            lease_store.close()

    async def test_pull_task_context_returns_latest_checkpoint_and_next_safe_action(self, monkeypatch):
        from pathlib import Path
        from app.services import task_lease_service as lease_mod

        lease_store = lease_mod.TaskLeaseStore(Path(":memory:"))
        monkeypatch.setattr(lease_mod, "_STORE", lease_store)

        async def fake_get(api_base: str, path: str):
            if path == "/project/tasks/task-1/statement?project=alpha":
                return {
                    "task": {"task_id": "task-1", "title": "Replay checkpoint task", "status": "active", "linked_improvement_id": "imp-1"},
                    "quality": {"capture_quality": "complete", "missing_artifacts": [], "grounded_by": ["task_changes"]},
                    "capture_review": {"pending_count": 0, "promoted_count": 1},
                    "next_actions": [{"priority": "low", "action": "Proceed from task statement.", "source_kind": "ready"}],
                }
            if path == "/project/tasks/task-1/changes?project=alpha&limit=100":
                return [
                    {
                        "id": "change-1",
                        "timestamp": "2026-04-25T10:00:00Z",
                        "tags": ["task_checkpoint", "task_stage:in_progress", "task_status:active"],
                        "content": "\n".join(
                            [
                                "[task_checkpoint]",
                                "Checkpoint stage: in_progress",
                                "Checkpoint status: active",
                                "Summary: Implemented checkpoint resume lookup.",
                                "Changed files: app/routers/mcp_sse.py",
                                "Verification: focused test passed",
                                "Next step: Add API-level test coverage.",
                            ]
                        ),
                    }
                ]
            raise AssertionError(path)

        async def fake_post(api_base: str, path: str, payload: dict):
            assert path == "/models/handoff/list"
            return {
                "handoffs": [
                    {
                        "task_id": "task-1",
                        "memory_id": "handoff-1",
                        "handoff_label": "checkpoint-task-1-blocked",
                        "status": "paused",
                    }
                ]
            }

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        try:
            result = await mcp_sse._execute_tool(
                "pull_task_context",
                {"project": "alpha", "task_id": "task-1", "agent_id": "codex"},
                "http://test",
            )

            assert '"task_id": "task-1"' in result
            assert '"latest_checkpoint"' in result
            assert '"next_safe_action": "Add API-level test coverage."' in result
            assert '"detail": "compact"' in result
            assert '"available_layers"' in result
            assert '"handoff_refs"' in result
            assert '"replay_completeness"' in result
            assert '"status": "complete"' in result
        finally:
            lease_store.close()

    async def test_checkpoint_replay_completeness_roundtrip_through_mcp_state(self, client, monkeypatch):
        from app.routers import models as models_router
        lease_store, work_token = _install_task_claim_store(monkeypatch, task_id="task-replay-1")

        class FakeRegistry:
            def log_handoff(self, **kwargs):
                pass

            def rank_for_task(self, task_type: str):
                return []

        async def local_get(api_base: str, path: str):
            response = await client.get(f"/api/v1{path}")
            assert response.status_code < 400, response.text
            return response.json()

        async def local_post(api_base: str, path: str, payload: dict):
            response = await client.post(f"/api/v1{path}", json=payload)
            assert response.status_code < 400, response.text
            return response.json()

        monkeypatch.setattr(mcp_sse, "_get", local_get)
        monkeypatch.setattr(mcp_sse, "_post", local_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())
        monkeypatch.setattr(models_router, "get_model_registry", lambda: FakeRegistry())

        try:
            create = await client.post(
                "/api/v1/project/tasks",
                json={
                    "project": "alpha",
                    "task_id": "task-replay-1",
                    "title": "Replay task from durable MCP state",
                    "description": "\n".join(
                        [
                            "Build a replay completeness check for mnemoforge task continuity.",
                            "Assumption: checkpoints are task changes, not a separate store.",
                            "Constraint: a new agent must not ask the user for old session context.",
                            "Definition of done: pull_task_context reconstructs next action from MCP state.",
                        ]
                    ),
                    "agent_id": "codex",
                    "status": "active",
                },
            )
            assert create.status_code == 201, create.text

            checkpoint = await mcp_sse._execute_tool(
                "record_task_checkpoint",
                {
                    "project": "alpha",
                    "task_id": "task-replay-1",
                    "stage": "handoff",
                    "status": "active",
                    "summary": "Implemented checkpoint replay plumbing.",
                    "decisions": ["Use a thin MCP facade over task changes and handoffs."],
                    "changed_files": ["app/routers/mcp_sse.py", "tests/test_mcp_sse.py"],
                    "verification": ["Replay roundtrip test exercises real task APIs."],
                    "remaining_risk": ["Full-project reproduction still needs broader scenario coverage."],
                    "next_step": "Add the full replay completeness scenario to the release gate.",
                    "acted_by": "codex",
                    "to_agent": "codex",
                    "session_id": "sess-1",
                    "work_token": work_token,
                },
                "http://test",
            )
            assert "Checkpoint recorded for task task-replay-1" in checkpoint
            assert "handoff_packet_created=True" in checkpoint
            active_claim = lease_store.get_active_claim(project="alpha", task_id="task-replay-1")
            assert active_claim is not None
            lease_store.release(
                lease_id=active_claim.lease_id,
                owner_agent="codex",
                session_id="sess-1",
                reason="test releases task before read-only replay",
            )

            replay = await mcp_sse._execute_tool(
                "pull_task_context",
                {"project": "alpha", "task_id": "task-replay-1", "agent_id": "codex", "detail": "full"},
                "http://test",
            )
            data = json.loads(replay)
        finally:
            lease_store.close()

        assert data["status"] == "ready"
        assert data["task"]["title"] == "Replay task from durable MCP state"
        assert data["latest_checkpoint"]["stage"] == "handoff"
        assert data["latest_checkpoint"]["summary"] == "Implemented checkpoint replay plumbing."
        assert data["latest_checkpoint"]["changed_files"] == ["app/routers/mcp_sse.py", "tests/test_mcp_sse.py"]
        assert data["latest_checkpoint"]["verification"] == ["Replay roundtrip test exercises real task APIs."]
        assert data["next_safe_action"] == "Add the full replay completeness scenario to the release gate."
        assert data["resume_handoffs"][0]["task_id"] == "task-replay-1"
        assert data["task_statement_quality"]["capture_quality"] in {"complete", "partial"}
        assert data["replay_completeness"] == {
            "status": "complete",
            "required_fields": [
                "task.title",
                "task.status",
                "latest_checkpoint.stage",
                "latest_checkpoint.summary",
                "latest_checkpoint.changed_files",
                "latest_checkpoint.verification",
                "next_safe_action",
            ],
            "missing_fields": [],
            "can_continue_without_user": True,
            "release_gate": "replay_completeness_v1",
        }

    async def test_pull_task_context_replay_completeness_flags_missing_checkpoint_fields(self, monkeypatch):
        from pathlib import Path
        from app.services import task_lease_service as lease_mod

        lease_store = lease_mod.TaskLeaseStore(Path(":memory:"))
        monkeypatch.setattr(lease_mod, "_STORE", lease_store)
        async def fake_get(api_base: str, path: str):
            if path == "/project/tasks/task-weak/statement?project=alpha":
                return {
                    "task": {"task_id": "task-weak", "title": "Weak replay task", "status": "active"},
                    "quality": {"capture_quality": "partial", "missing_artifacts": ["verification_result"]},
                    "capture_review": {"pending_count": 0, "promoted_count": 0},
                    "next_actions": [{"priority": "high", "action": "Record a planning checkpoint.", "source_kind": "missing_context"}],
                }
            if path == "/project/tasks/task-weak/changes?project=alpha&limit=100":
                return []
            raise AssertionError(path)

        async def fake_post(api_base: str, path: str, payload: dict):
            assert path == "/models/handoff/list"
            return {"handoffs": []}

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        try:
            result = await mcp_sse._execute_tool(
                "pull_task_context",
                {"project": "alpha", "task_id": "task-weak", "agent_id": "codex"},
                "http://test",
            )
            data = json.loads(result)

            assert data["replay_completeness"]["status"] == "incomplete"
            assert data["replay_completeness"]["can_continue_without_user"] is False
            assert data["replay_completeness"]["missing_fields"] == [
                "latest_checkpoint.stage",
                "latest_checkpoint.summary",
                "latest_checkpoint.changed_files",
                "latest_checkpoint.verification",
            ]
            assert data["recommended_first_tool"] == "record_task_checkpoint"
        finally:
            lease_store.close()

    async def test_pull_task_context_returns_occupied_when_active_lease_exists(self, monkeypatch):
        from pathlib import Path
        from app.services import task_lease_service as lease_mod

        store = lease_mod.TaskLeaseStore(Path(":memory:"))
        monkeypatch.setattr(lease_mod, "_STORE", store)
        try:
            store.claim(
                project="alpha",
                task_id="task-1",
                owner_agent="claude",
                session_id="sess-claude",
                lease_ttl_seconds=60,
            )
            result = await mcp_sse._execute_tool(
                "pull_task_context",
                {"project": "alpha", "task_id": "task-1"},
                "http://test",
            )
            data = json.loads(result)
            assert data["status"] == "occupied"
            assert data["occupied_by"]["owner_agent"] == "claude"
            assert data["occupied_by"]["owner_session_id"] == "sess-claude"
        finally:
            store.close()

    async def test_continue_task_returns_removed_tool_error(self, monkeypatch):
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())
        result = await mcp_sse._execute_tool(
            "continue_task",
            {"project": "alpha", "task_id": "task-1"},
            "http://test",
        )
        data = json.loads(result)
        assert data["status"] == "error"
        assert data["error"] == "tool_removed_use_pull_task_context"

    async def test_record_task_checkpoint_requires_active_owned_claim(self, monkeypatch):
        from pathlib import Path
        from app.services import task_lease_service as lease_mod

        store = lease_mod.TaskLeaseStore(Path(":memory:"))
        monkeypatch.setattr(lease_mod, "_STORE", store)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())
        try:
            result = await mcp_sse._execute_tool(
                "record_task_checkpoint",
                {
                    "project": "alpha",
                    "task_id": "task-1",
                    "stage": "in_progress",
                    "summary": "Started implementation.",
                    "status": "active",
                    "session_id": "sess-codex",
                    "acted_by": "codex",
                },
                "http://test",
            )
            data = json.loads(result)
            assert data["status"] == "conflict"
            assert data["error"] == "active_claim_required"
        finally:
            store.close()

    async def test_record_task_checkpoint_denies_when_claim_owned_by_other_session(self, monkeypatch):
        from pathlib import Path
        from app.services import task_lease_service as lease_mod

        store = lease_mod.TaskLeaseStore(Path(":memory:"))
        monkeypatch.setattr(lease_mod, "_STORE", store)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())
        try:
            store.claim(project="alpha", task_id="task-1", owner_agent="claude", session_id="sess-claude")
            result = await mcp_sse._execute_tool(
                "record_task_checkpoint",
                {
                    "project": "alpha",
                    "task_id": "task-1",
                    "stage": "in_progress",
                    "summary": "Started implementation.",
                    "status": "active",
                    "session_id": "sess-codex",
                    "acted_by": "codex",
                },
                "http://test",
            )
            data = json.loads(result)
            assert data["status"] == "conflict"
            assert data["error"] == "lease_owner_mismatch"
            assert data["owner_agent"] == "claude"
            assert data["owner_session_id"] == "sess-claude"
        finally:
            store.close()

    async def test_project_workflow_returns_task_completion_form(self, monkeypatch):
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "project_workflow",
            {
                "project": "alpha",
                "intent": "I finished task task-1 and ran tests",
                "task_id": "task-1",
            },
            "http://test",
        )

        assert '"workflow": "task_completion"' in result
        assert '"submit_tool": "project_workflow_submit"' in result
        assert '"artifact_key": "task:alpha:task-1"' in result
        assert '"completion_summary": ""' in result

    async def test_project_workflow_submit_records_completion_and_resolves_artifact(self, monkeypatch):
        posted: list[tuple[str, dict]] = []

        async def fake_post(api_base: str, path: str, payload: dict):
            posted.append((path, payload))
            if path.endswith("/changes"):
                return {"id": "change-1", "task_id": "task-1"}
            if path.endswith("/resolve"):
                return {"artifact_key": "task:alpha:task-1", "status": "done"}
            raise AssertionError(path)

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "project_workflow_submit",
            {
                "workflow": "task_completion",
                "acted_by": "codex",
                "form": {
                    "project": "alpha",
                    "task_id": "task-1",
                    "completion_summary": "Implemented the completion workflow.",
                    "changed_files": ["app/routers/mcp_sse.py"],
                    "tests_run": ["pytest tests/test_mcp_sse.py -k project_workflow"],
                    "test_result": "passed",
                    "verdict": "completed",
                    "residual_risks": [],
                    "should_resolve_artifact": True,
                },
            },
            "http://test",
        )

        assert posted[0][0] == "/project/tasks/task-1/changes"
        assert "Implemented the completion workflow." in posted[0][1]["content"]
        assert "Tests run: pytest tests/test_mcp_sse.py -k project_workflow" in posted[0][1]["content"]
        assert posted[1][0] == "/artifacts/task%3Aalpha%3Atask-1/resolve"
        assert posted[1][1]["acted_by"] == "codex"
        assert '"routed_to": [' in result
        assert '"resolve_artifact"' in result

    async def test_reopen_task_tool_calls_project_task_reopen_endpoint(self, monkeypatch):
        calls: list[tuple[str, str, dict]] = []

        async def fake_post(api_base: str, path: str, payload: dict):
            calls.append((api_base, path, payload))
            return {
                "id": "task-1",
                "task_id": "task-1",
                "project": "alpha",
                "status": "active",
                "changes": [],
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "reopen_task",
            {"task_id": "task-1", "status": "active", "reason": "manual_resume", "acted_by": "codex"},
            "http://test",
        )

        assert calls == [("http://test", "/project/tasks/task-1/reopen", {"status": "active", "reason": "manual_resume", "acted_by": "codex", "source": "mcp"})]
        assert '"status": "active"' in result

    async def test_tool_recommend_merges_project_recommendations(self, monkeypatch):
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        async def fake_post(api_base: str, path: str, payload: dict):
            assert path == "/project/enrich-task"
            return {
                "context": "## Recommended MCP Calls\n\n1. `list_open_tasks`",
                "recommended_mcp_calls": [
                    {
                        "tool": "list_open_tasks",
                        "reason": "Project-specific open work items should be inspected first.",
                        "args": {"project": "alpha", "limit": 10},
                    }
                ],
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)

        result = await mcp_sse._execute_tool(
            "tool_recommend",
            {"task": "Find the right MCP call for open work items", "project_id": "alpha", "top_n": 2},
            "http://test",
        )

        assert '"stage": "testing"' in result
        assert '"feedback_expected": true' in result
        assert '"follow_up": "tool_feedback"' in result
        assert '"tool": "list_open_tasks"' in result
        assert '"project_recommended_calls"' in result
        assert '"project_context_summary"' in result

    async def test_tool_recommend_prefers_pull_task_context_for_checkpoint_resume_requests(self, monkeypatch):
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "tool_recommend",
            {"task": "Resume task 84c4e534-d722-4132-8660-4a56ed93f44a from checkpoint"},
            "http://test",
        )

        assert '"tool": "pull_task_context"' in result
        assert '"tool": "start_task_session"' in result
        assert '"tool": "normalize_mcp_intent"' in result
        assert '"canonical_surface"' in result
        assert '"stage": "testing"' in result
        assert '"feedback_expected": true' in result
        assert '"follow_up": "tool_feedback"' in result
        assert result.index('"tool": "pull_task_context"') < result.index('"tool": "start_task_session"')

    async def test_tool_recommend_prefers_reopen_task_for_reactivation_requests(self, monkeypatch):
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "tool_recommend",
            {"task": "Reopen task 84c4e534-d722-4132-8660-4a56ed93f44a"},
            "http://test",
        )

        assert '"tool": "reopen_task"' in result
        assert '"tool": "normalize_mcp_intent"' in result
        assert result.index('"tool": "reopen_task"') < result.index('"tool": "report_task_checkpoint"')

    async def test_tool_explain_marks_testing_tools_for_feedback(self, monkeypatch):
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "tool_explain",
            {"tool_name": "tool_feedback", "task_context": "close the loop after tool use"},
            "http://test",
        )

        assert '"stage": "testing"' in result
        assert '"feedback_expected": true' in result
        assert '"follow_up": "tool_feedback"' in result

    async def test_tool_feedback_records_learning_ledger_entry(self, monkeypatch):
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        class _FakeStore:
            def __init__(self):
                self.feedback_calls = []
                self.event_calls = []

            async def write_feedback(self, **kwargs):
                self.feedback_calls.append(kwargs)
                return 42

            async def write_event(self, **kwargs):
                self.event_calls.append(kwargs)
                return 7

        fake_store = _FakeStore()
        from app.services import learning_store

        recorded = {}

        def fake_record_tool_feedback(**kwargs):
            recorded.update(kwargs)
            return {"tool_name": kwargs.get("tool_name"), "stage": kwargs.get("tool_stage")}

        monkeypatch.setattr(learning_store, "get_learning_store", lambda: fake_store)
        monkeypatch.setattr(mcp_sse, "record_tool_feedback", fake_record_tool_feedback)

        result = await mcp_sse._execute_tool(
            "tool_feedback",
            {
                "tool_name": "tool_family_tools",
                "tool_stage": "testing",
                "valence": "positive",
                "worked": True,
                "scope": "tool discovery",
                "what_was_tested": "Family browsing for a large MCP catalog",
                "expected_behavior": "Return a compact family-level view with follow-up guidance.",
                "observed_behavior": "Returned family metadata and a clear next step.",
                "friction": "Need clearer stage markers.",
                "missing_fields": ["stage", "feedback_expected"],
                "suggestion": "Add stage to the family tool listing.",
                "next_action": "Retest with the new envelope and compare whether agents need fewer follow-up reads.",
                "task_context": "Verify tool discovery feedback flow",
                "project_id": "alpha",
                "agent_id": "codex",
                "session_id": "sess-123",
            },
            "http://test",
            session_id="sess-123",
        )

        assert '"summary": "Recorded tool feedback for tool_family_tools"' in result
        assert '"assessment": "keep_testing"' in result
        assert '"scope": "tool discovery"' in result
        assert '"what_was_tested": "Family browsing for a large MCP catalog"' in result
        assert '"expected_behavior": "Return a compact family-level view with follow-up guidance."' in result
        assert '"observed_behavior": "Returned family metadata and a clear next step."' in result
        assert '"next_action": "Retest with the new envelope and compare whether agents need fewer follow-up reads."' in result
        assert fake_store.feedback_calls
        fb = fake_store.feedback_calls[0]
        assert fb["source"] == "mcp_tool_feedback"
        assert fb["payload"]["tool_name"] == "tool_family_tools"
        assert fb["payload"]["tool_stage"] == "testing"
        assert recorded["tool_name"] == "tool_family_tools"
        assert recorded["tool_stage"] == "testing"
        assert fake_store.event_calls
        assert fake_store.event_calls[0]["event_type"] == "artifact_feedback"

    async def test_initialize_exposes_mnemoforge_operational_guidance(self):
        response = await mcp_sse._handle(
            {
                "jsonrpc": "2.0",
                "id": "1",
                "method": "initialize",
                "params": {"clientInfo": {"name": "Codex CLI"}},
            },
            "http://test",
            session_id="sess-1",
        )

        info = response["result"]["_mnemoforge"]
        assert info["agent_id"] == "codex-cli"
        assert "help, state, get, submit" in info["tip"]
        assert "Do not start by reading client config files" in info["tip"]
        assert "mcp_settings.json" in info["tip"]
        assert "alwaysAllow" in info["tip"]
        assert "Legacy and specialized tools are compatibility/debug surfaces" in info["tip"]
        assert info["tool_catalog"]["preferred_mode"] == "compact"
        assert info["tool_catalog"]["compact_request"] == {"method": "tools/list", "params": {"mode": "compact"}}
        assert info["tool_catalog"]["full_request"] == {"method": "tools/list", "params": {"mode": "full"}}
        assert info["tool_catalog"]["recommended_first_tool"] == "help"
        assert info["tool_catalog"]["public_surface"] == ["help", "state", "get", "submit"]
        assert "mcp_settings.json" in info["tool_catalog"]["do_not_bootstrap_from"]
        assert "simple public MCP surface" in info["tool_catalog"]["reason"]
        assert any("/api/v1/coordination/" in line for line in info["semantic_defaults"])
        assert any("project-specific hints" in line for line in info["semantic_defaults"])
        assert any("alwaysAllow" in line for line in info["semantic_defaults"])
        assert "get with a task ref" in info["semantic_defaults"][3]

    async def test_get_onboarding_includes_mnemoforge_basics(self, monkeypatch):
        async def fake_get(api_base: str, path: str):
            if path == "/admin/storage-trust":
                return {
                    "status": "degraded",
                    "summary": "Storage trust is degraded: at least one integrity slice is unhealthy and operator action is required.",
                }
            if path == "/skills/pinned":
                return []
            if path.startswith("/skills/gaps"):
                return {"gaps": []}
            if path.startswith("/skills/analytics"):
                return {"total_outcomes": 0}
            if path.startswith("/memories/recent"):
                return []
            raise AssertionError(f"unexpected GET path: {path}")

        async def fake_post(api_base: str, path: str, payload: dict):
            if path == "/skills/profile":
                return {"domains": ["automation"]}
            if path == "/skills/pack/create":
                return {
                    "pack_id": "pack-1",
                    "degraded": True,
                    "degraded_reason": "Qdrant skill/domain filter is degraded; skill pack may be served from fallback storage.",
                    "skills": [{"id": "skill-1", "name": "remember-context", "description": "Keep project context stable"}],
                }
            raise AssertionError(f"unexpected POST path: {path}")

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())
        monkeypatch.setattr(
            mcp_sse,
            "get_active_operational_instincts",
            lambda *args, **kwargs: [
                {
                    "rank": 1,
                    "instinct_id": "trust_first",
                    "action": "Trust storage and project context first.",
                    "family": "ops",
                    "phase": "onboarding",
                },
            ],
        )

        result = await mcp_sse._execute_tool(
            "get_onboarding",
            {"agent_id": "codex", "task_description": "Work with remote project coordination"},
            "http://test",
        )

        # New layered instruction format
        assert "L0: Core Policy" in result
        assert "L1: Task Context" in result
        assert "L2: Coordination Guidelines" in result
        assert "ACTIVE OPERATIONAL INSTINCTS:" in result
        assert "trust_first" in result
        assert "get_storage_trust_status" in result
        assert "STORAGE TRUST WARNING:" in result
        assert "INTEGRITY WARNING:" in result
        assert "EXPERT HELPER GUIDANCE:" in result
        assert "Public surface first: help, state, get, submit" in result
        assert "Do not bootstrap from mcp_settings.json" in result
        assert "alwaysAllow" in result
        assert "Start project work with state" in result
        assert "project-specific hints" in result
        assert "get with task:<project>:<task_id>" in result
        assert "use reopen_task only" in result

    async def test_get_onboarding_degrades_gracefully_when_skill_pack_http500(self, monkeypatch):
        async def fake_get(api_base: str, path: str):
            if path == "/admin/storage-trust":
                return {"status": "ok", "summary": "ok"}
            if path == "/skills/pinned":
                return []
            if path.startswith("/skills/gaps"):
                return {"gaps": []}
            if path.startswith("/skills/analytics"):
                return {"total_outcomes": 0}
            if path.startswith("/memories/recent"):
                return []
            raise AssertionError(f"unexpected GET path: {path}")

        async def fake_post(api_base: str, path: str, payload: dict):
            if path == "/skills/profile":
                return {"domains": ["automation"]}
            if path == "/skills/pack/create":
                req = httpx.Request("POST", f"{api_base}{path}")
                resp = httpx.Response(
                    500,
                    request=req,
                    json={"detail": "Qdrant domain_tags filter panic"},
                )
                raise httpx.HTTPStatusError("Server error", request=req, response=resp)
            raise AssertionError(f"unexpected POST path: {path}")

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())
        monkeypatch.setattr(
            mcp_sse,
            "get_active_operational_instincts",
            lambda *args, **kwargs: [
                {
                    "rank": 1,
                    "instinct_id": "trust_first",
                    "action": "Trust storage and project context first.",
                    "family": "ops",
                    "phase": "onboarding",
                },
            ],
        )

        result = await mcp_sse._execute_tool(
            "get_onboarding",
            {"agent_id": "codex", "task_description": "Need orientation"},
            "http://test",
        )

        # New layered instruction format
        assert "L0: Core Policy" in result
        assert "L1: Task Context" in result
        assert "L2: Memory Operations Guidelines" in result
        assert "Skills: temporarily unavailable (HTTP 500: Qdrant domain_tags filter panic)." in result
        assert "/skills/pack/create" not in result

    async def test_list_pending_handoff_labels_formats_named_queue(self, monkeypatch):
        async def fake_get(api_base: str, path: str):
            assert path == "/models/handoff/pending_labels?agent_id=claude-code&limit=20"
            return {
                "agent_id": "claude-code",
                "found": 2,
                "labels": [
                    {
                        "handoff_label": "benchmark28",
                        "count": 1,
                        "latest_task_id": "abc12345",
                        "from_agents": ["codex"],
                    },
                    {
                        "handoff_label": "tailcutoff371",
                        "count": 2,
                        "latest_task_id": "def67890",
                        "from_agents": ["codex", "cline"],
                    },
                ],
            }

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "list_pending_handoff_labels",
            {"agent_id": "claude-code", "limit": 20},
            "http://test",
        )

        assert "Pending handoff labels for 'claude-code':" in result
        assert "benchmark28" in result
        assert "tailcutoff371" in result

    async def test_pickup_handoff_suggests_label_listing_when_multiple_items_arrive(self, monkeypatch):
        async def fake_post(api_base: str, path: str, payload: dict):
            assert path == "/models/handoff/pickup"
            return {
                "agent_id": "claude-code",
                "handoff_label": None,
                "found": 2,
                "handoffs": [
                    {
                        "task_id": "abc12345",
                        "handoff_label": "benchmark28",
                        "from_agent": "codex",
                        "memory_id": "mem-1",
                        "content": "HANDOFF CONTEXT\n...",
                    },
                    {
                        "task_id": "def67890",
                        "handoff_label": "tailcutoff371",
                        "from_agent": "codex",
                        "memory_id": "mem-2",
                        "content": "HANDOFF CONTEXT\n...",
                    },
                ],
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "pickup_handoff",
            {"agent_id": "claude-code", "limit": 5},
            "http://test",
        )

        assert "Found 2 pending handoff(s) for 'claude-code':" in result
        assert "list_pending_handoff_labels(agent_id='claude-code')" in result
        assert "handoff_label: benchmark28" in result

    async def test_pickup_handoff_renders_structured_phase_snapshot(self, monkeypatch):
        async def fake_post(api_base: str, path: str, payload: dict):
            assert path == "/models/handoff/pickup"
            return {
                "agent_id": "claude-code",
                "handoff_label": "benchmark28",
                "found": 1,
                "handoffs": [
                    {
                        "task_id": "abc12345",
                        "handoff_label": "benchmark28",
                        "from_agent": "codex",
                        "memory_id": "mem-1",
                        "status": "pending",
                        "project_id": "mnemoforge",
                        "owner_agent": "claude-code",
                        "write_scope": ["handoff", "task-packet"],
                        "phase": "task_framing",
                        "priority": "high",
                        "definition_of_done": "Produce a planning packet",
                        "expected_output_shape": "Short ranked benchmark plan",
                        "phase_objective": "Turn an incomplete request into an explicit task.",
                        "core_instinct_ids": [
                            "assume_initial_task_statement_is_incomplete",
                            "clarify_scope_assumptions_and_done",
                        ],
                        "supporting_instinct_ids": ["track_assumptions_explicitly"],
                        "project_context_summary": "coverage laws=1, components=1; highlights laws: Require review",
                        "project_context_refs": {"laws": ["law-1"], "components": ["handoff"]},
                        "content": "HANDOFF CONTEXT\n...",
                    }
                ],
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "pickup_handoff",
            {"agent_id": "claude-code", "handoff_label": "benchmark28", "limit": 5},
            "http://test",
        )

        assert "status: pending" in result
        assert "project_id: mnemoforge" in result
        assert "owner_agent: claude-code" in result
        assert "write_scope: handoff, task-packet" in result
        assert "phase: task_framing" in result
        assert "priority: high" in result
        assert "definition_of_done: Produce a planning packet" in result
        assert "expected_output_shape: Short ranked benchmark plan" in result
        assert "phase_objective: Turn an incomplete request into an explicit task." in result
        assert "core_instinct_ids: assume_initial_task_statement_is_incomplete, clarify_scope_assumptions_and_done" in result
        assert "supporting_instinct_ids: track_assumptions_explicitly" in result
        assert "project_context_summary: coverage laws=1, components=1; highlights laws: Require review" in result
        assert "project_context_refs: laws=1, components=1" in result
        assert "Use expand_handoff_refs(memory_id='mem-1') to inspect referenced context." in result
        assert "laws=law-1" not in result

    async def test_expand_handoff_refs_formats_compact_resolution(self, monkeypatch):
        async def fake_post(api_base: str, path: str, payload: dict):
            assert path == "/models/handoff/expand_refs"
            return {
                "memory_id": "mem-1",
                "project_id": "mnemoforge",
                "requested_ref_types": ["laws", "components"],
                "resolved": {
                    "laws": [
                        {
                            "id": "law-1",
                            "title": "Require review",
                            "status": "active",
                            "statement": "Explicit review is required.",
                        }
                    ],
                    "components": [
                        {
                            "component_id": "handoff",
                            "name": "Handoff",
                            "summary": "Carry portable task context.",
                        }
                    ],
                },
                "unresolved": {"docs_sections": ["overview"]},
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "expand_handoff_refs",
            {"memory_id": "mem-1", "ref_types": ["laws", "components"], "limit_per_type": 2},
            "http://test",
        )

        assert "Expanded handoff refs for mem-1" in result
        assert "project_id: mnemoforge" in result
        assert "requested_ref_types: laws, components" in result
        assert "- law-1 [active] Require review" in result
        assert "- handoff Handoff: Carry portable task context." in result
        assert "unresolved: docs_sections=1" in result

    async def test_expand_handoff_refs_formats_task_capture_candidates(self, monkeypatch):
        async def fake_post(api_base: str, path: str, payload: dict):
            assert path == "/models/handoff/expand_refs"
            return {
                "memory_id": "mem-2",
                "project_id": "mnemoforge",
                "requested_ref_types": ["task_capture_candidates"],
                "resolved": {
                    "task_capture_candidates": [
                        {
                            "artifact_id": "capture-1",
                            "task_id": "task-1",
                            "kind": "assumption",
                            "source": "local_slm",
                            "status": "active",
                            "content": "Task framing is still incomplete until assumption capture is reviewed.",
                        }
                    ]
                },
                "unresolved": {},
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "expand_handoff_refs",
            {"memory_id": "mem-2", "ref_types": ["task_capture_candidates"], "limit_per_type": 2},
            "http://test",
        )

        assert "requested_ref_types: task_capture_candidates" in result
        assert "- capture-1 [active] assumption for task-1: Task framing is still incomplete" in result

    async def test_refresh_handoff_context_formats_refresh_result(self, monkeypatch):
        async def fake_post(api_base: str, path: str, payload: dict):
            assert path == "/models/handoff/refresh_context"
            return {
                "memory_id": "mem-1",
                "status": "picked_up",
                "project_id": "mnemoforge",
                "owner_agent": "claude-code",
                "write_scope": ["handoff"],
                "task_description": "Refresh this task packet",
                "project_context_summary": "coverage laws=1, components=1, improvements=1, tasks=1, docs_sections=1",
                "project_context_refs": {
                    "laws": ["law-1"],
                    "components": ["handoff"],
                    "improvements": ["imp-1"],
                    "tasks": ["task-1"],
                    "docs_sections": ["overview"],
                },
                "coverage": {
                    "laws": 1,
                    "components": 1,
                    "improvements": 1,
                    "runtime_hints": 0,
                    "tasks": 1,
                    "docs_sections": 1,
                },
                "code_inspection_recommended": False,
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "refresh_handoff_context",
            {"memory_id": "mem-1", "task_description": "Refresh this task packet", "max_components": 2},
            "http://test",
        )

        assert "Refreshed handoff context for mem-1" in result
        assert "status: picked_up" in result
        assert "project_id: mnemoforge" in result
        assert "owner_agent: claude-code" in result
        assert "write_scope: handoff" in result
        assert "task: Refresh this task packet" in result
        assert "project_context_summary: coverage laws=1, components=1, improvements=1, tasks=1, docs_sections=1" in result
        assert "project_context_refs: laws=1, components=1, improvements=1, tasks=1, docs_sections=1" in result
        assert "coverage: laws=1, components=1, improvements=1, tasks=1, docs_sections=1" in result

    async def test_handoff_workspace_summary_formats_compact_overview(self, monkeypatch):
        response = {
            "agent_id": "codex",
            "statuses": ["active", "paused"],
            "handoff_label": "benchmark28",
            "owner_agent": "claude-code",
            "write_scope": ["handoff", "task-packet"],
            "total": 2,
            "by_status": {"active": 1, "paused": 1},
            "by_owner_agent": {"claude-code": 1, "codex": 1},
            "by_phase": {"task_framing": 1, "pre_implementation": 1},
            "merge_back_guidance": "Merge back into the active packet after review; avoid opening a new branch unless ownership changes.",
            "recent_packets": [
                {
                    "task_id": "abc12345",
                    "handoff_label": "benchmark28",
                    "status": "active",
                    "owner_agent": "claude-code",
                    "phase": "task_framing",
                    "priority": "high",
                    "executor_used": "cheap-worker",
                    "model_used": "qwen3:1.7b",
                    "memory_id": "mem-1",
                },
                {
                    "task_id": "def67890",
                    "status": "paused",
                    "owner_agent": "codex",
                    "phase": "pre_implementation",
                    "priority": "medium",
                    "executor_used": "codex-mini",
                    "model_used": "gpt-5.1-mini",
                    "memory_id": "mem-2",
                },
            ],
        }

        async def fake_sse_post(api_base: str, path: str, payload: dict):
            assert path == "/models/handoff/workspace_summary"
            assert payload == {
                "agent_id": "codex",
                "statuses": ["active", "paused"],
                "handoff_label": "benchmark28",
                "owner_agent": "claude-code",
                "write_scope": ["handoff", "task-packet"],
                "packet_limit": 5,
            }
            return response

        monkeypatch.setattr(mcp_sse, "_post", fake_sse_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        sse_result = await mcp_sse._execute_tool(
            "handoff_workspace_summary",
            {
                "agent_id": "codex",
                "statuses": ["active", "paused"],
                "handoff_label": "benchmark28",
                "owner_agent": "claude-code",
                "write_scope": ["handoff", "task-packet"],
                "packet_limit": 5,
            },
            "http://test",
        )

        assert "Workspace handoff summary for 'codex' (active, paused):" in sse_result
        assert "total: 2" in sse_result
        assert "by_status: active=1, paused=1" in sse_result
        assert "by_owner_agent: claude-code=1, codex=1" in sse_result
        assert "by_phase: task_framing=1, pre_implementation=1" in sse_result
        assert "merge_back_guidance: Merge back into the active packet after review; avoid opening a new branch unless ownership changes." in sse_result
        assert "- task_id=abc12345 label=benchmark28 status=active owner_agent=claude-code phase=task_framing priority=high memory_id=mem-1 executor_used=cheap-worker model_used=qwen3:1.7b" in sse_result
        assert "- task_id=def67890 status=paused owner_agent=codex phase=pre_implementation priority=medium memory_id=mem-2 executor_used=codex-mini model_used=gpt-5.1-mini" in sse_result
        assert "pending_labels:" not in sse_result

    async def test_update_handoff_status_formats_packet_lifecycle_change(self, monkeypatch):
        async def fake_post(api_base: str, path: str, payload: dict):
            assert path == "/models/handoff/status"
            return {
                "memory_id": "mem-1",
                "status": "active",
                "acted_by": "codex",
                "reason": "resumed for implementation",
                "owner_agent": "claude-code",
                "write_scope": ["handoff", "task-packet"],
                "executor_used": "cheap-worker",
                "model_used": "qwen3:1.7b",
                "result_summary": "Added the bounded packet change.",
                "verification_summary": "Focused tests passed.",
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "update_handoff_status",
            {
                "memory_id": "mem-1",
                "status": "active",
                "acted_by": "codex",
                "reason": "resumed for implementation",
            },
            "http://test",
        )

        assert "Updated handoff status for mem-1" in result
        assert "status: active" in result
        assert "owner_agent: claude-code" in result
        assert "write_scope: handoff, task-packet" in result
        assert "executor_used: cheap-worker" in result
        assert "model_used: qwen3:1.7b" in result
        assert "result_summary: Added the bounded packet change." in result
        assert "verification_summary: Focused tests passed." in result
        assert "acted_by: codex" in result
        assert "reason: resumed for implementation" in result

    async def test_list_handoffs_formats_status_aware_packet_listing(self, monkeypatch):
        async def fake_post(api_base: str, path: str, payload: dict):
            assert path == "/models/handoff/list"
            return {
                "agent_id": "claude-code",
                "statuses": ["active", "paused"],
                "found": 2,
                "handoffs": [
                    {
                        "task_id": "abc12345",
                        "handoff_label": "resume42",
                        "status": "paused",
                        "owner_agent": "claude-code",
                        "write_scope": ["handoff", "task-packet"],
                        "result_summary": "Prepared the bounded patch.",
                        "executor_used": "cheap-worker",
                        "model_used": "qwen3:1.7b",
                        "phase": "task_framing",
                        "priority": "high",
                        "memory_id": "mem-1",
                    },
                    {
                        "task_id": "def67890",
                        "handoff_label": "implement88",
                        "status": "active",
                        "owner_agent": "codex",
                        "write_scope": ["implementation"],
                        "phase": "pre_implementation",
                        "priority": "medium",
                        "executor_used": "codex-mini",
                        "model_used": "gpt-5.1-mini",
                        "memory_id": "mem-2",
                    },
                ],
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "list_handoffs",
            {"agent_id": "claude-code", "statuses": ["active", "paused"], "limit": 20},
            "http://test",
        )

        assert "Handoffs for 'claude-code' (active, paused):" in result
        assert "label=resume42 status=paused phase=task_framing priority=high memory_id=mem-1 owner_agent=claude-code write_scope=handoff, task-packet executor_used=cheap-worker model_used=qwen3:1.7b result_summary=Prepared the bounded patch." in result
        assert "label=implement88 status=active phase=pre_implementation priority=medium memory_id=mem-2 owner_agent=codex write_scope=implementation executor_used=codex-mini model_used=gpt-5.1-mini" in result

    async def test_resume_handoff_formats_reactivation_result(self, monkeypatch):
        async def fake_post(api_base: str, path: str, payload: dict):
            assert path == "/models/handoff/resume"
            return {
                "memory_id": "mem-1",
                "status": "active",
                "refreshed": True,
                "acted_by": "codex",
                "reason": "returning to task",
                "project_id": "mnemoforge",
                "owner_agent": "claude-code",
                "write_scope": ["handoff", "task-packet"],
                "phase": "pre_implementation",
                "priority": "medium",
                "task_description": "Resume this task packet",
                "phase_objective": "Continue bounded implementation work.",
                "definition_of_done": "Implement the next compact slice.",
                "expected_output_shape": "Short implementation result with verification.",
                "project_context_summary": "coverage laws=1, components=1, improvements=1, tasks=1, docs_sections=1",
                "project_context_refs": {
                    "laws": ["law-1"],
                    "components": ["handoff"],
                    "improvements": ["imp-1"],
                    "tasks": ["task-1"],
                    "docs_sections": ["overview"],
                },
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "resume_handoff",
            {
                "memory_id": "mem-1",
                "refresh_context": True,
                "acted_by": "codex",
                "reason": "returning to task",
                "max_components": 2,
            },
            "http://test",
        )

        assert "Resumed handoff mem-1" in result
        assert "status: active" in result
        assert "refreshed: true" in result
        assert "owner_agent: claude-code" in result
        assert "write_scope: handoff, task-packet" in result
        assert "acted_by: codex" in result
        assert "reason: returning to task" in result
        assert "project_id: mnemoforge" in result
        assert "phase: pre_implementation" in result
        assert "priority: medium" in result
        assert "task: Resume this task packet" in result
        assert "phase_objective: Continue bounded implementation work." in result
        assert "definition_of_done: Implement the next compact slice." in result
        assert "expected_output_shape: Short implementation result with verification." in result
        assert "project_context_refs: laws=1, components=1, improvements=1, tasks=1, docs_sections=1" in result

    async def test_decompose_task_packet_formats_recommended_packet_stubs(self, monkeypatch):
        response = {
            "project_id": "mnemoforge",
            "strategy": "split_by_write_scope",
            "recommended_packet_count": 2,
            "phase": "pre_implementation",
            "phase_objective": "Deliver a bounded implementation slice that is easy to verify and merge back.",
            "why_split": "Suggested packets are separated by bounded write scopes so they can be delegated with lower conflict risk.",
            "packets": [
                {
                    "handoff_label": "packet-models-py",
                    "owner_agent": "codex",
                    "phase": "pre_implementation",
                    "priority": "high",
                    "execution_mode": "balanced",
                    "suggested_execution_tier": "cheap",
                    "model_hint": "gpt-5.1-mini",
                    "write_scope": ["app/routers/models.py"],
                    "definition_of_done": "Finish the bounded work for this packet, verify the result, and return a short merge-back summary.",
                    "expected_output_shape": "Short result summary, verification summary, and any follow-up or merge-back notes.",
                },
                {
                    "handoff_label": "packet-mcp_sse-py",
                    "owner_agent": "nash",
                    "phase": "pre_implementation",
                    "priority": "high",
                    "execution_mode": "economy",
                    "suggested_execution_tier": "cheap",
                    "model_hint": "gpt-5.1-mini",
                    "write_scope": ["app/routers/mcp_sse.py", "mcp/server.py"],
                    "definition_of_done": "Finish the bounded work for this packet, verify the result, and return a short merge-back summary.",
                    "expected_output_shape": "Short result summary, verification summary, and any follow-up or merge-back notes.",
                },
            ],
        }

        async def fake_sse_post(api_base: str, path: str, payload: dict):
            assert path == "/models/handoff/decompose"
            assert payload == {
                "project_id": "mnemoforge",
                "task_description": "Split packet work",
                "priority": "high",
                "write_scope": ["app/routers/models.py", "app/routers/mcp_sse.py", "mcp/server.py"],
                "max_packets": 2,
            }
            return response

        monkeypatch.setattr(mcp_sse, "_post", fake_sse_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        sse_result = await mcp_sse._execute_tool(
            "decompose_task_packet",
            {
                "project_id": "mnemoforge",
                "task_description": "Split packet work",
                "priority": "high",
                "write_scope": ["app/routers/models.py", "app/routers/mcp_sse.py", "mcp/server.py"],
                "max_packets": 2,
            },
            "http://test",
        )

        assert "Task packet decomposition:" in sse_result
        assert "strategy: split_by_write_scope" in sse_result
        assert "recommended_packet_count: 2" in sse_result
        assert "label=packet-models-py owner_agent=codex phase=pre_implementation priority=high execution_mode=balanced suggested_execution_tier=cheap model_hint=gpt-5.1-mini write_scope=app/routers/models.py" in sse_result
        assert "label=packet-mcp_sse-py owner_agent=nash phase=pre_implementation priority=high execution_mode=economy suggested_execution_tier=cheap model_hint=gpt-5.1-mini write_scope=app/routers/mcp_sse.py, mcp/server.py" in sse_result

    async def test_create_task_packets_formats_created_packet_bundle(self, monkeypatch):
        response = {
            "project_id": "mnemoforge",
            "created_count": 2,
            "packets": [
                {
                    "task_id": "task-1",
                    "handoff_label": "packet-models-py",
                    "memory_id": "mem-1",
                    "to_agent": "codex",
                    "status": "pending",
                    "owner_agent": "codex",
                    "write_scope": ["app/routers/models.py"],
                    "phase": "pre_implementation",
                    "priority": "high",
                    "execution_mode": "balanced",
                    "suggested_execution_tier": "cheap",
                    "model_hint": "gpt-5.1-mini",
                    "why_now": "Keep the models patch isolated.",
                    "definition_of_done": "Finish the bounded work and return a short merge-back summary.",
                    "expected_output_shape": "Short result summary and verification summary.",
                    "phase_objective": "Deliver a bounded implementation slice that is easy to verify and merge back.",
                    "core_instinct_ids": ["every_task_must_exist_in_memory"],
                    "supporting_instinct_ids": ["clarify_scope_assumptions_and_done"],
                    "pickup_instruction": "In codex: use pickup_handoff(agent_id='codex', handoff_label='packet-models-py')",
                },
                {
                    "task_id": "task-2",
                    "handoff_label": "packet-mcp_sse-py",
                    "memory_id": "mem-2",
                    "to_agent": "nash",
                    "status": "pending",
                    "owner_agent": "nash",
                    "write_scope": ["app/routers/mcp_sse.py", "mcp/server.py"],
                    "phase": "pre_implementation",
                    "priority": "high",
                    "execution_mode": "economy",
                    "suggested_execution_tier": "cheap",
                    "model_hint": "gpt-5.1-mini",
                    "definition_of_done": "Finish the bounded work and return a short merge-back summary.",
                    "expected_output_shape": "Short result summary and verification summary.",
                    "pickup_instruction": "In nash: use pickup_handoff(agent_id='nash', handoff_label='packet-mcp_sse-py')",
                },
            ],
        }

        expected_payload = {
            "from_agent": "codex",
            "to_agent": "claude-code",
            "project_id": "mnemoforge",
            "task_description": "Split packet work",
            "partial_result": "Preflight ready",
            "key_facts": ["one", "two"],
            "reason": "manual",
            "from_model_id": "gpt-4o",
            "agent_id": "handoff",
            "packets": [
                {
                    "handoff_label": "packet-models-py",
                    "owner_agent": "codex",
                    "write_scope": ["app/routers/models.py"],
                    "phase": "pre_implementation",
                    "priority": "high",
                    "execution_mode": "balanced",
                    "suggested_execution_tier": "cheap",
                    "model_hint": "gpt-5.1-mini",
                    "why_now": "Keep the models patch isolated.",
                    "definition_of_done": "Finish the bounded work and return a short merge-back summary.",
                    "expected_output_shape": "Short result summary and verification summary.",
                    "phase_objective": "Deliver a bounded implementation slice that is easy to verify and merge back.",
                    "core_instinct_ids": ["every_task_must_exist_in_memory"],
                    "supporting_instinct_ids": ["clarify_scope_assumptions_and_done"],
                    "project_context_summary": "coverage laws=1, components=1",
                    "project_context_refs": {"laws": ["law-1"], "components": ["handoff"]},
                    "project_context_snapshot": "HANDOFF CONTEXT",
                },
                {
                    "handoff_label": "packet-mcp_sse-py",
                    "owner_agent": "nash",
                    "write_scope": ["app/routers/mcp_sse.py", "mcp/server.py"],
                    "phase": "pre_implementation",
                    "priority": "high",
                    "execution_mode": "economy",
                    "suggested_execution_tier": "cheap",
                    "model_hint": "gpt-5.1-mini",
                    "definition_of_done": "Finish the bounded work and return a short merge-back summary.",
                    "expected_output_shape": "Short result summary and verification summary.",
                },
            ],
        }

        async def fake_sse_post(api_base: str, path: str, payload: dict):
            assert path == "/models/handoff/create_packets"
            assert payload == expected_payload
            return response

        monkeypatch.setattr(mcp_sse, "_post", fake_sse_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        sse_result = await mcp_sse._execute_tool(
            "create_task_packets",
            {
                "from_agent": "codex",
                "to_agent": "claude-code",
                "project_id": "mnemoforge",
                "task_description": "Split packet work",
                "partial_result": "Preflight ready",
                "key_facts": ["one", "two"],
                "reason": "manual",
                "from_model_id": "gpt-4o",
                "agent_id": "handoff",
                "packets": [
                {
                    "handoff_label": "packet-models-py",
                    "owner_agent": "codex",
                    "write_scope": ["app/routers/models.py"],
                    "phase": "pre_implementation",
                    "priority": "high",
                    "execution_mode": "balanced",
                    "suggested_execution_tier": "cheap",
                    "model_hint": "gpt-5.1-mini",
                        "why_now": "Keep the models patch isolated.",
                        "definition_of_done": "Finish the bounded work and return a short merge-back summary.",
                        "expected_output_shape": "Short result summary and verification summary.",
                        "phase_objective": "Deliver a bounded implementation slice that is easy to verify and merge back.",
                        "core_instinct_ids": ["every_task_must_exist_in_memory"],
                        "supporting_instinct_ids": ["clarify_scope_assumptions_and_done"],
                        "project_context_summary": "coverage laws=1, components=1",
                        "project_context_refs": {"laws": ["law-1"], "components": ["handoff"]},
                        "project_context_snapshot": "HANDOFF CONTEXT",
                    },
                {
                    "handoff_label": "packet-mcp_sse-py",
                    "owner_agent": "nash",
                    "write_scope": ["app/routers/mcp_sse.py", "mcp/server.py"],
                    "phase": "pre_implementation",
                    "priority": "high",
                    "execution_mode": "economy",
                    "suggested_execution_tier": "cheap",
                    "model_hint": "gpt-5.1-mini",
                        "definition_of_done": "Finish the bounded work and return a short merge-back summary.",
                        "expected_output_shape": "Short result summary and verification summary.",
                    },
                ],
            },
            "http://test",
        )
        assert "Created 2 task packet(s)" in sse_result
        assert "handoff_label: packet-models-py" in sse_result
        assert "handoff_label: packet-mcp_sse-py" in sse_result
        assert "owner_agent: codex" in sse_result
        assert "write_scope: app/routers/models.py" in sse_result
        assert "write_scope: app/routers/mcp_sse.py, mcp/server.py" in sse_result
        assert "execution_mode: balanced" in sse_result
        assert "execution_mode: economy" in sse_result
        assert "suggested_execution_tier: cheap" in sse_result
        assert "model_hint: gpt-5.1-mini" in sse_result

    async def test_route_task_packet_execution_formats_compact_recommendation(self, monkeypatch):
        async def fake_post(api_base: str, path: str, payload: dict):
            assert path == "/models/handoff/route_execution"
            assert payload["memory_id"] == "mem-1"
            assert payload["packet"]["task_description"] == "Implement cheap-tier MCP parity"
            assert payload["packet"]["write_scope"] == ["app/routers/mcp_sse.py", "mcp/server.py"]
            assert payload["packet"]["execution_mode"] == "strict_economy"
            return {
                "memory_id": "mem-1",
                "packet": payload["packet"],
                "packet_profile": {"tier": "cheap", "score": 0.93, "reason": "small bounded packet"},
                "routing_basis": ["cheap-tier executor is sufficient", "write scope is narrow"],
                "eligible_executors": ["cheap-worker", "codex-mini"],
                "recommended_executor": "cheap-worker",
                "recommended_model": "gemini-2.5-flash",
                "recommendation_reason": "packet fits the cheap-tier executor and stays within the write scope",
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "route_task_packet_execution",
            {
                "memory_id": "mem-1",
                "packet": {
                    "task_description": "Implement cheap-tier MCP parity",
                    "write_scope": ["app/routers/mcp_sse.py", "mcp/server.py"],
                    "phase": "task_framing",
                    "execution_mode": "strict_economy",
                    "suggested_execution_tier": "cheap",
                    "model_hint": "qwen3:1.7b",
                    "executor_used": "cheap-worker",
                    "model_used": "qwen3:1.7b",
                    "definition_of_done": "Tool is available in both transports",
                    "expected_output_shape": "Compact routing recommendation",
                },
            },
            "http://test",
        )

        assert "Route execution recommendation:" in result
        assert "memory_id: mem-1" in result
        assert "packet: task_description=Implement cheap-tier MCP parity" in result
        assert "phase=task_framing" in result
        assert "execution_mode=strict_economy" in result
        assert "suggested_execution_tier=cheap" in result
        assert "model_hint=qwen3:1.7b" in result
        assert "executor_used=cheap-worker" in result
        assert "model_used=qwen3:1.7b" in result
        assert "write_scope=app/routers/mcp_sse.py, mcp/server.py" in result
        assert "definition_of_done: Tool is available in both transports" in result
        assert "expected_output_shape: Compact routing recommendation" in result
        assert "packet_profile: tier=cheap; score=0.93; reason=small bounded packet" in result
        assert "routing_basis: cheap-tier executor is sufficient, write scope is narrow" in result
        assert "eligible_executors: cheap-worker, codex-mini" in result
        assert "recommended_executor: cheap-worker" in result
        assert "recommended_model: gemini-2.5-flash" in result
        assert "recommendation_reason: packet fits the cheap-tier executor" in result

    async def test_dispatch_background_task_packet_formats_job_submission(self, monkeypatch):
        async def fake_post(api_base: str, path: str, payload: dict):
            assert path == "/models/handoff/dispatch_background"
            assert payload == {
                "memory_id": "mem-1",
                "acted_by": "codex",
                "reason": "queue the low-risk packet",
            }
            return {
                "memory_id": "mem-1",
                "status": "active",
                "executor_used": "local_slm_background",
                "model_used": "qwen3:1.7b",
                "background_job_type": "docs_rebuild",
                "job_id": "job-docs-1",
                "poll": "/api/v1/tasks/job-docs-1",
                "recommendation_reason": "Strict economy mode prefers local background execution first for low-risk packets that are easy to verify.",
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "dispatch_background_task_packet",
            {
                "memory_id": "mem-1",
                "acted_by": "codex",
                "reason": "queue the low-risk packet",
            },
            "http://test",
        )

        assert "Background dispatch queued:" in result
        assert "memory_id: mem-1" in result
        assert "status: active" in result
        assert "executor_used: local_slm_background" in result
        assert "model_used: qwen3:1.7b" in result
        assert "background_job_type: docs_rebuild" in result
        assert "job_id: job-docs-1" in result
        assert "poll: /api/v1/tasks/job-docs-1" in result

    async def test_reconcile_background_task_packet_formats_job_state(self, monkeypatch):
        async def fake_post(api_base: str, path: str, payload: dict):
            assert path == "/models/handoff/reconcile_background"
            assert payload == {"memory_id": "mem-1", "acted_by": "codex"}
            return {
                "memory_id": "mem-1",
                "status": "closed",
                "job_id": "job-docs-1",
                "background_job_status": "done",
                "background_job_type": "docs_rebuild",
                "executor_used": "local_slm_background",
                "model_used": "qwen3:1.7b",
                "result_summary": "Background job docs_rebuild completed.",
                "verification_summary": "project=mnemoforge; sections=overview, api",
                "poll": "/api/v1/tasks/job-docs-1",
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "reconcile_background_task_packet",
            {"memory_id": "mem-1", "acted_by": "codex"},
            "http://test",
        )

        assert "Background job reconciled:" in result
        assert "status: closed" in result
        assert "background_job_status: done" in result
        assert "background_job_type: docs_rebuild" in result
        assert "executor_used: local_slm_background" in result
        assert "model_used: qwen3:1.7b" in result
        assert "result_summary: Background job docs_rebuild completed." in result
        assert "verification_summary: project=mnemoforge; sections=overview, api" in result
        assert "poll: /api/v1/tasks/job-docs-1" in result

    async def test_list_handoffs_surfaces_background_job_state(self, monkeypatch):
        async def fake_post(api_base: str, path: str, payload: dict):
            assert path == "/models/handoff/list"
            return {
                "found": 1,
                "statuses": ["closed"],
                "handoffs": [
                    {
                        "task_id": "task-1",
                        "handoff_label": "resume42",
                        "status": "closed",
                        "phase": "task_framing",
                        "priority": "high",
                        "memory_id": "mem-1",
                        "background_job_status": "done",
                        "dispatched_job_id": "job-docs-1",
                    }
                ],
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "list_handoffs",
            {"agent_id": "codex", "statuses": ["closed"], "limit": 10},
            "http://test",
        )

        assert "background_job_status=done" in result
        assert "dispatched_job_id=job-docs-1" in result

    async def test_handoff_task_includes_phase_and_iteration_contract(self, monkeypatch):
        async def fake_post(api_base: str, path: str, payload: dict):
            if path == "/project/enrich-task":
                assert payload["project_id"] == "mnemoforge"
                assert payload["task"] == "Benchmark competitors"
                return {
                    "context": "## Relevant Components\n\n### Handoff\n**Purpose:** Carry portable task context.\n",
                    "components": [{"component_id": "handoff", "name": "Handoff"}],
                    "laws": [{"id": "law-1", "title": "Require review"}],
                    "improvements": [{"id": "imp-1", "title": "Tighten handoff packet"}],
                    "runtime_hints": [],
                    "memoirs": [],
                    "tasks": [{"task_id": "task-1", "title": "Benchmark flow"}],
                    "docs_sections": [{"section_key": "overview"}],
                    "operational_instincts": [],
                    "missing_sources": [],
                    "code_inspection_recommended": False,
                    "message": "",
                }
            assert path == "/models/handoff"
            assert payload["project_id"] == "mnemoforge"
            assert payload["phase"] == "task_framing"
            assert payload["priority"] == "high"
            assert payload["definition_of_done"] == "Produce a planning packet"
            assert payload["expected_output_shape"] == "Short ranked benchmark plan"
            assert payload["phase_objective"]
            assert "every_task_must_exist_in_memory" in payload["core_instinct_ids"]
            assert payload["project_context_summary"].startswith("coverage laws=1, components=1, improvements=1, tasks=1, docs_sections=1")
            assert payload["project_context_refs"] == {
                "laws": ["law-1"],
                "components": ["handoff"],
                "improvements": ["imp-1"],
                "tasks": ["task-1"],
                "docs_sections": ["overview"],
            }
            assert payload["owner_agent"] == "claude-code"
            assert payload["write_scope"] == ["handoff", "task-packet"]
            return {
                "memory_id": "mem-1",
                "task_id": "abc12345",
                "handoff_label": "benchmark28",
                "project_id": "mnemoforge",
                "owner_agent": "claude-code",
                "write_scope": ["handoff", "task-packet"],
                "phase": "task_framing",
                "priority": "high",
                "phase_objective": "Turn an incomplete request into an explicit, bounded, and explainable task.",
                "core_instinct_ids": [
                    "assume_initial_task_statement_is_incomplete",
                    "clarify_scope_assumptions_and_done",
                    "every_task_must_exist_in_memory",
                ],
                "project_context_summary": "coverage laws=1, components=1, improvements=1, tasks=1, docs_sections=1; highlights laws: Require review | components: Handoff | improvements: Tighten handoff packet",
                "project_context_refs": {
                    "laws": ["law-1"],
                    "components": ["handoff"],
                    "improvements": ["imp-1"],
                    "tasks": ["task-1"],
                    "docs_sections": ["overview"],
                },
                "to_agent": "claude-code",
                "next_available": [{"model_id": "gpt-4o"}],
                "pickup_instruction": "In claude-code: use pickup_handoff(agent_id='claude-code', handoff_label='benchmark28')",
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "handoff_task",
            {
                "from_agent": "codex",
                "to_agent": "claude-code",
                "task_description": "Benchmark competitors",
                "project_id": "mnemoforge",
                "owner_agent": "claude-code",
                "write_scope": ["handoff", "task-packet"],
                "handoff_label": "benchmark28",
                "phase": "task_framing",
                "priority": "high",
                "definition_of_done": "Produce a planning packet",
                "expected_output_shape": "Short ranked benchmark plan",
            },
            "http://test",
        )

        assert "handoff_label: benchmark28" in result
        assert "owner_agent: claude-code" in result
        assert "write_scope: handoff, task-packet" in result
        assert "phase: task_framing" in result
        assert "priority: high" in result
        assert "phase_objective:" in result
        assert "core_instinct_ids:" in result
        assert "project_context_summary:" in result
        assert "project_context_refs: laws=1, components=1, improvements=1, tasks=1, docs_sections=1" in result

    async def test_registry_best_lists_all_ranked_components(self, monkeypatch):
        async def fake_get(api_base: str, path: str):
            return {
                "task_type": "code_review",
                "ranked": [
                    {"component": "cloud-llm", "score": 0.91},
                    {"component": "qwen3:1.7b", "score": 0.44},
                ],
            }

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "registry_best",
            {"task_type": "code_review", "top": 2},
            "http://test",
        )

        assert "1. cloud-llm" in result
        assert "2. qwen3:1.7b" in result

    async def test_list_project_laws_tool_uses_laws_endpoint(self, monkeypatch):
        seen: dict[str, str] = {}

        async def fake_get(api_base: str, path: str):
            seen["path"] = path
            return {
                "items": [
                    {
                        "id": "law-1",
                        "title": "Require explicit approval",
                        "status": "active",
                        "scope": "project",
                        "project": "alpha",
                        "is_project_local": True,
                    }
                ]
            }

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "list_project_laws",
            {"project": "alpha", "status": "active", "limit": 10},
            "http://test",
        )

        assert seen["path"] == "/laws?status=active&limit=10&include_promoted=true&project=alpha"
        assert "Require explicit approval" in result

    async def test_get_project_law_tool_formats_law(self, monkeypatch):
        async def fake_get(api_base: str, path: str):
            assert path == "/laws/law-1"
            return {
                "id": "law-1",
                "title": "Require explicit approval",
                "status": "active",
                "scope": "project",
                "project": "alpha",
                "version": "1.0",
                "statement": "Do not deploy without approval.",
                "rationale": "Deploys are high-risk.",
                "evidence": ["incident-1"],
            }

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "get_project_law",
            {"law_id": "law-1"},
            "http://test",
        )

        assert "Require explicit approval" in result
        assert "incident-1" in result

    async def test_sse_list_learning_candidates_uses_pending_review_endpoint(self, monkeypatch):
        seen: dict[str, str] = {}

        async def fake_get(api_base: str, path: str):
            seen["path"] = path
            return {
                "artifacts": [
                    {
                        "id": "cand-1",
                        "content": "No skill guidance found for 'ssl termination'.",
                        "evidence_count": 3,
                        "confidence": 0.8,
                        "risk_level": "low",
                        "meta": {"signal_type": "skill_gap"},
                    }
                ]
            }

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "list_learning_candidates",
            {"limit": 5},
            "http://test",
        )

        assert seen["path"] == "/learning/artifacts?scope=candidate&status=pending_review&limit=5"
        assert "cand-1" in result
        assert "ssl termination" in result

    async def test_sse_approve_learning_candidate_uses_review_endpoint(self, monkeypatch):
        seen: dict[str, object] = {}

        async def fake_post(api_base: str, path: str, payload: dict):
            seen["path"] = path
            seen["payload"] = payload
            return {
                "id": "cand-1",
                "status": "active",
                "artifact_scope": "runtime_hint",
                "meta": {
                    "approved_by": "user",
                    "approval_source": "dashboard_review",
                    "approval_reason": "Looks good.",
                },
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "approve_learning_candidate",
            {"artifact_id": "cand-1", "approved_by": "user", "approval_source": "dashboard_review", "reason": "Looks good."},
            "http://test",
        )

        assert seen["path"] == "/learning/candidates/cand-1/approve"
        assert seen["payload"] == {
            "approved_by": "user",
            "approval_source": "dashboard_review",
            "reason": "Looks good.",
        }
        assert "approved learning candidate cand-1" in result
        assert "approval_source=dashboard_review" in result

    async def test_sse_get_project_readiness_uses_project_readiness_endpoint(self, monkeypatch):
        seen: dict[str, object] = {}

        async def fake_post(api_base: str, path: str, payload: dict):
            seen["path"] = path
            seen["payload"] = payload
            return {
                "project_id": "alpha",
                "readiness_level": "limited_pilot_ready",
                "readiness_score": 72,
                "coverage": {
                    "components": 1,
                    "laws": 0,
                    "improvements": 1,
                    "runtime_hints": 0,
                    "memoirs": 0,
                    "tasks": 1,
                    "docs_sections": 1,
                },
                "blocking_gaps": [],
                "recommended_actions": ["Optionally import or define initial project laws for stronger governance and context."],
                "strengths": ["Component knowledge is indexed."],
                "code_inspection_recommended": False,
                "summary": "Project 'alpha' is limited_pilot_ready (72/100).",
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "get_project_readiness",
            {"project_id": "alpha"},
            "http://test",
        )

        assert seen["path"] == "/project/readiness"
        assert seen["payload"] == {"project_id": "alpha"}
        assert "Project readiness for alpha: limited_pilot_ready (72/100)" in result
        assert "Coverage: components=1" in result

    async def test_sse_get_project_bootstrap_checklist_uses_bootstrap_endpoint(self, monkeypatch):
        seen: dict[str, object] = {}

        async def fake_post(api_base: str, path: str, payload: dict):
            seen["path"] = path
            seen["payload"] = payload
            return {
                "project_id": "alpha",
                "readiness_level": "bootstrap_needed",
                "bootstrap_ready": False,
                "next_step": "components_indexed",
                "summary": "Bootstrap checklist for 'alpha': bootstrap_needed. Next step: components_indexed.",
                "steps": [
                    {
                        "step_id": "components_indexed",
                        "title": "Index or refresh component knowledge",
                        "required": True,
                        "status": "pending",
                        "action": "Ingest the project or refresh indexed components.",
                        "tool_hint": "project/ingest or project/refresh",
                    }
                ],
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "get_project_bootstrap_checklist",
            {"project_id": "alpha"},
            "http://test",
        )

        assert seen["path"] == "/project/bootstrap-checklist"
        assert seen["payload"] == {"project_id": "alpha"}
        assert "Bootstrap checklist for alpha: bootstrap_needed" in result
        assert "components_indexed" in result

    async def test_sse_get_project_reconstruction_bundle_uses_reconstruction_endpoint(self, monkeypatch):
        seen: dict[str, object] = {}

        async def fake_post(api_base: str, path: str, payload: dict):
            seen["path"] = path
            seen["payload"] = payload
            return {
                "project_id": "alpha",
                "source_policy": {
                    "project_agnostic": True,
                    "source_code_required": False,
                    "uses_governed_memory_layers": True,
                    "raw_memories_are_not_primary_knowledge": True,
                },
                "reconstruction_readiness": {
                    "status": "partial",
                    "missing_layers": [],
                    "warning_layers": ["laws"],
                },
                "coverage": {
                    "components": 2,
                    "laws": 0,
                    "improvements": 3,
                    "runtime_hints": 1,
                    "memoirs": 1,
                    "tasks": 4,
                    "docs_sections": 2,
                },
                "layers": [
                    {
                        "layer": "components",
                        "count": 2,
                        "role": "Recover implementation boundaries.",
                        "items": [{"component_id": "api", "name": "API"}],
                    }
                ],
                "reconstruction_sequence": ["Assess storage trust.", "Recover product intent."],
                "next_actions": ["Run a dry reconstruction drill."],
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "get_project_reconstruction_bundle",
            {"project_id": "alpha", "detail": "compact", "max_items_per_layer": 3},
            "http://test",
        )

        assert seen["path"] == "/project/reconstruction-bundle"
        assert seen["payload"] == {"project_id": "alpha", "detail": "compact", "max_items_per_layer": 3}
        assert "Project reconstruction bundle for alpha: partial" in result
        assert "source_code_required=False" in result
        assert "components: count=2" in result
        assert "Run a dry reconstruction drill." in result

    async def test_sse_plan_remote_snapshot_uses_project_plan_endpoint(self, monkeypatch):
        seen: dict[str, object] = {}

        async def fake_post(api_base: str, path: str, payload: dict):
            seen["path"] = path
            seen["payload"] = payload
            return {
                "project_id": "alpha",
                "storage_mode": "knowledge_only",
                "snapshot": {
                    "source_mode": "git_snapshot",
                    "repo": "https://github.com/example/alpha",
                    "branch": "main",
                    "commit_sha": "abc123",
                    "base_commit_sha": "abc123",
                    "dirty_workspace": False,
                },
                "counts": {
                    "changed_files": 1,
                    "deleted_files": 0,
                    "renamed_files": 0,
                    "files_with_content": 0,
                },
                "plan": {
                    "rebuild_mode": "skip_if_unchanged",
                    "projection_target_state": "effective",
                    "requires_selective_source_payload": True,
                    "can_skip_when_unchanged": True,
                    "touched_paths": ["app/context.py"],
                },
                "contract": {
                    "stores_selective_source_cache": False,
                    "full_mirror_enabled": False,
                },
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "plan_remote_snapshot",
            {
                "project_id": "alpha",
                "storage_mode": "knowledge_only",
                "snapshot": {"source_mode": "git_snapshot", "commit_sha": "abc123"},
                "changed_files": ["app/context.py"],
            },
            "http://test",
        )

        assert seen["path"] == "/project/remote-snapshot/plan"
        assert seen["payload"]["project_id"] == "alpha"
        assert "Remote snapshot plan for alpha: rebuild_mode=skip_if_unchanged" in result
        assert "Touched paths: app/context.py" in result

    async def test_sse_sync_remote_snapshot_uses_project_sync_endpoint(self, monkeypatch):
        seen: dict[str, object] = {}

        async def fake_post(api_base: str, path: str, payload: dict):
            seen["path"] = path
            seen["payload"] = payload
            return {
                "project_id": "alpha",
                "action": "needs_source_payload",
                "plan": {
                    "plan": {
                        "rebuild_mode": "diff_only",
                        "projection_target_state": "effective",
                        "requires_selective_source_payload": True,
                    }
                },
                "refresh": {
                    "updated": [],
                    "up_to_date": [],
                    "requires_source_payload": ["context"],
                    "used_remote_file_payload": False,
                },
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "sync_remote_snapshot",
            {
                "project_id": "alpha",
                "storage_mode": "knowledge_only",
                "snapshot": {"source_mode": "git_snapshot", "commit_sha": "def456"},
                "changed_files": ["app/context.py"],
            },
            "http://test",
        )

        assert seen["path"] == "/project/remote-snapshot/sync"
        assert seen["payload"]["project_id"] == "alpha"
        assert "Remote snapshot sync for alpha: action=needs_source_payload" in result
        assert "Components needing source payload: context" in result

    async def test_sse_get_storage_trust_status_uses_admin_storage_trust(self, monkeypatch):
        seen: dict[str, object] = {}

        async def fake_get(api_base: str, path: str):
            seen["path"] = path
            return {
                "status": "degraded",
                "summary": "Storage trust is degraded: at least one integrity slice is unhealthy and operator action is required.",
                "signals": {
                    "degraded_slices": ["qdrant.skill_domain_tags_filter"],
                    "active_hygiene_findings": 948,
                    "manual_review_pending": {"synthetic_test": 23},
                    "quarantine_candidates": {},
                    "delete_ready": {},
                },
                "next_actions": ["Investigate degraded integrity slices before trusting affected storage filters and retrieval paths."],
            }

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool("get_storage_trust_status", {}, "http://test")

        assert seen["path"] == "/admin/storage-trust"
        assert "Storage trust: degraded" in result
        assert "qdrant.skill_domain_tags_filter" in result

    async def test_sse_send_coordination_message_uses_models_coordination_endpoint(self, monkeypatch):
        seen: dict[str, object] = {}

        async def fake_post(api_base: str, path: str, payload: dict):
            seen["path"] = path
            seen["payload"] = payload
            return {
                "memory_id": "msg-1",
                "project": "alpha",
                "thread_id": "thread-1",
                "from_agent": "codex",
                "to_agent": "claude-code",
                "message_type": "question",
                "content": "Need file ownership clarification.",
                "status": "new",
                "priority": "normal",
                "requested_action": "",
                "response_to_message_id": "",
                "source": "mcp_coordination",
                "tags": ["project:alpha"],
                "timestamp": "2026-03-22T12:00:00Z",
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "send_coordination_message",
            {"project": "alpha", "from_agent": "codex", "to_agent": "claude-code", "content": "Need file ownership clarification."},
            "http://test",
        )

        assert seen["path"] == "/models/coordination/messages"
        assert seen["payload"]["project"] == "alpha"
        assert "msg-1" in result
        assert "codex" in result


class TestAsyncioNoiseFilter:
    def test_suppresses_winerror_10054_connection_lost_noise(self):
        exc = ConnectionResetError(10054, "remote host closed connection")
        exc.winerror = 10054
        context = {
            "message": "Exception in callback _ProactorBasePipeTransport._call_connection_lost(None)",
            "exception": exc,
            "handle": _FakeHandle(),
        }
        assert _should_suppress_asyncio_transport_error(context) is True

    def test_keeps_other_asyncio_errors_visible(self):
        context = {
            "message": "Exception in callback some_other_handler",
            "exception": RuntimeError("boom"),
        }
        assert _should_suppress_asyncio_transport_error(context) is False
