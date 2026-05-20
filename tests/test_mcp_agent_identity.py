from __future__ import annotations

import json

from app.services.mcp_agent_identity import (
    build_agent_fingerprint,
    build_fingerprint_from_identity,
    load_or_create_agent_identity,
)


def test_load_or_create_agent_identity_persists_local_uuid(tmp_path) -> None:
    path = tmp_path / ".mnemoforge" / "agent_identity.json"

    first = load_or_create_agent_identity(
        path=path,
        client_name="Codex CLI",
        runtime_profile_id="strong_mcp_operator",
    )
    second = load_or_create_agent_identity(
        path=path,
        client_name="Other CLI",
        runtime_profile_id="weak_mcp_operator",
    )

    assert first["local_agent_uuid"]
    assert second["local_agent_uuid"] == first["local_agent_uuid"]
    assert second["client_name"] == "Codex CLI"
    assert json.loads(path.read_text(encoding="utf-8"))["local_agent_uuid"] == first["local_agent_uuid"]


def test_build_agent_fingerprint_is_stable_and_workspace_local(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    first = build_agent_fingerprint(
        workspace_root=workspace,
        client_name="Codex CLI",
        model_name="gpt-5",
        runtime_profile_id="strong_mcp_operator",
        local_agent_uuid="local-agent-1",
    )
    second = build_agent_fingerprint(
        workspace_root=workspace,
        client_name="codex cli",
        model_name="GPT-5",
        runtime_profile_id="strong_mcp_operator",
        local_agent_uuid="local-agent-1",
    )
    other_model = build_agent_fingerprint(
        workspace_root=workspace,
        client_name="Codex CLI",
        model_name="small-local-model",
        runtime_profile_id="strong_mcp_operator",
        local_agent_uuid="local-agent-1",
    )

    assert first.startswith("agentfp:")
    assert first == second
    assert other_model != first


def test_build_fingerprint_from_identity_uses_persisted_defaults(tmp_path) -> None:
    identity = {
        "local_agent_uuid": "local-agent-1",
        "client_name": "Codex CLI",
        "runtime_profile_id": "weak_mcp_operator",
    }

    from_identity = build_fingerprint_from_identity(
        identity,
        workspace_root=tmp_path,
        model_name="gpt-5",
    )
    direct = build_agent_fingerprint(
        workspace_root=tmp_path,
        client_name="Codex CLI",
        model_name="gpt-5",
        runtime_profile_id="weak_mcp_operator",
        local_agent_uuid="local-agent-1",
    )

    assert from_identity == direct
