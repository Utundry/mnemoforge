from pathlib import Path

from app.services import publish_readiness_service as service


def test_publish_readiness_reports_missing_status_doc_and_doc_sanitization_issues(tmp_path: Path, monkeypatch):
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "README.md").write_text("# Supermemory\n\n## Quick start\n", encoding="utf-8")
    (tmp_path / "SETUP.md").write_text("Broken Ð setup with D:\\work\\supermemory and 192.168.1.10", encoding="utf-8")
    (tmp_path / "CLIENT_SETUP.md").write_text("Client doc with /home/user/supermemory", encoding="utf-8")
    (tmp_path / "demo").mkdir(parents=True, exist_ok=True)
    (tmp_path / "Dockerfile").write_text("FROM python:3.11", encoding="utf-8")
    (tmp_path / "docker-compose.yml").write_text("services: {}", encoding="utf-8")
    (tmp_path / ".env.example").write_text("API_KEY=\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text(".env\n.venv/\nqdrant_data/\nlogs/\n*.db\n.server.pid\n", encoding="utf-8")
    (tmp_path / "docs" / "PROJECT_KNOWLEDGE_MODEL.md").write_text("# Architecture\n", encoding="utf-8")
    (tmp_path / "docs" / "EXTERNAL_PROJECT_ROADMAP.md").write_text("# Roadmap\n", encoding="utf-8")

    monkeypatch.setattr(service, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        service,
        "build_functionality_alpha_config",
        lambda: {
            "status": "warning",
            "disabled_modules": ["layout_fixer", "openai_compat"],
        "disabled_modules_env": "layout_fixer,openai_compat",
        "default_surface": ["memories", "mcp_sse"],
    },
    )

    result = service.build_publish_readiness()

    assert result["status"] == "warning"
    assert result["publish_target"] == "github_alpha"
    assert "status_doc" in result["public_docs"]["missing"]
    assert "demo_readme" in result["demo_dataset"]["missing"]
    assert "DISABLED_MODULES" in result["env_example"]["missing_keys"]
    assert "SETUP.md" in result["sanitization"]["issues"]["mojibake_docs"]
    assert "SETUP.md" in result["sanitization"]["issues"]["local_path_docs"]
    assert "SETUP.md" in result["sanitization"]["issues"]["private_network_docs"]
    assert result["alpha_surface"]["disabled_modules_env"] == "layout_fixer,openai_compat"
    assert result["blockers"]
    assert result["warnings"]


def test_publish_readiness_can_report_clean_ok_state(tmp_path: Path, monkeypatch):
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "demo").mkdir(parents=True, exist_ok=True)
    (tmp_path / "README.md").write_text("# Supermemory\n\n## Quick start\n", encoding="utf-8")
    (tmp_path / "SETUP.md").write_text("Setup guide\n", encoding="utf-8")
    (tmp_path / "CLIENT_SETUP.md").write_text("Client guide\n", encoding="utf-8")
    (tmp_path / "STATUS.md").write_text("# Alpha Status\n", encoding="utf-8")
    (tmp_path / "demo" / "README.md").write_text("# Demo dataset\n", encoding="utf-8")
    (tmp_path / "demo" / "demo_memories.jsonl").write_text("{\"content\":\"demo\",\"agent_id\":\"demo\",\"memory_type\":\"fact\"}\n", encoding="utf-8")
    (tmp_path / "Dockerfile").write_text("FROM python:3.11", encoding="utf-8")
    (tmp_path / "docker-compose.yml").write_text("services: {}", encoding="utf-8")
    (tmp_path / ".env.example").write_text(
        "SELF_PROJECT_ID=supermemory\nDISABLED_MODULES=layout_fixer\nAPI_KEY=\n",
        encoding="utf-8",
    )
    (tmp_path / ".gitignore").write_text(".env\n.venv/\nqdrant_data/\nlogs/\n*.db\n.server.pid\n", encoding="utf-8")
    (tmp_path / "docs" / "PROJECT_KNOWLEDGE_MODEL.md").write_text("# Architecture\n", encoding="utf-8")
    (tmp_path / "docs" / "EXTERNAL_PROJECT_ROADMAP.md").write_text("# Roadmap\n", encoding="utf-8")

    monkeypatch.setattr(service, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        service,
        "build_functionality_alpha_config",
        lambda: {
            "status": "ok",
        "disabled_modules": ["layout_fixer"],
        "disabled_modules_env": "layout_fixer",
        "default_surface": ["memories", "mcp_sse"],
    },
    )

    result = service.build_publish_readiness()

    assert result["status"] == "ok"
    assert result["package_presence"]["missing"] == []
    assert result["public_docs"]["missing"] == []
    assert result["demo_dataset"]["missing"] == []
    assert result["env_example"]["missing_keys"] == []
    assert result["sanitization"]["issues"]["mojibake_docs"] == []
    assert result["sanitization"]["issues"]["local_path_docs"] == []
    assert result["sanitization"]["issues"]["private_network_docs"] == []
