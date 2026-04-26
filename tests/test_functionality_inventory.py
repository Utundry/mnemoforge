from pathlib import Path

from app.services import functionality_inventory_service as service
from app.services.functionality_inventory_service import (
    build_functionality_alpha_config,
    bootstrap_functionality_review_hints,
    build_functionality_inventory,
    build_functionality_release_scope,
    build_functionality_review_dossier,
    build_functionality_review_queue,
    list_functionality_review_hints,
    upsert_functionality_review_hint,
)


def test_functionality_inventory_reports_review_pressure_and_release_blockers(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(service, "_DB_PATH", tmp_path / "functionality_review.db")
    bootstrap_functionality_review_hints()
    result = build_functionality_inventory()

    assert result["inventory_version"] == 1
    assert result["total_modules"] >= 10
    assert result["status"] == "warning"
    assert result["summary"]["review_pressure"] >= 1
    assert "watcher" in result["summary"]["release_blockers"]
    assert "layout_fixer" in result["summary"]["release_blockers"]
    assert any(item["module"] == "project" and item["status"] == "keep" for item in result["items"])
    assert any(item["module"] == "mcp_sse" and item["surface_kind"] == "transport" for item in result["items"])
    assert result["next_actions"]


def test_functionality_release_scope_groups_modules_for_alpha_positioning(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(service, "_DB_PATH", tmp_path / "functionality_review.db")
    bootstrap_functionality_review_hints()
    result = build_functionality_release_scope()

    assert result["status"] == "warning"
    assert "memories" in result["default_surface"]
    assert "mcp_sse" in result["default_surface"]
    assert "dashboard" in result["modernize_before_alpha"]
    assert "layout_fixer" in result["candidate_feature_flags"]
    assert "watcher" in result["deprecate_review"]
    assert result["next_actions"]


def test_functionality_review_hints_can_be_bootstrapped_and_overridden(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(service, "_DB_PATH", tmp_path / "functionality_review.db")

    seeded = bootstrap_functionality_review_hints()
    assert seeded["total_seeded"] >= 10
    assert seeded["created"] >= 10

    hints = list_functionality_review_hints()
    assert any(item["module"] == "watcher" for item in hints)

    updated = upsert_functionality_review_hint(
        module="watcher",
        status="keep",
        reason="validated for this test scope",
    )
    assert updated["module"] == "watcher"

    overridden = build_functionality_inventory()
    watcher = next(item for item in overridden["items"] if item["module"] == "watcher")
    assert watcher["status"] == "keep"


def test_functionality_review_dossier_reports_code_facts_for_module(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(service, "_DB_PATH", tmp_path / "functionality_review.db")
    bootstrap_functionality_review_hints()
    dossier = build_functionality_review_dossier("watcher")

    assert dossier["module"] == "watcher"
    assert dossier["file_path"].endswith("app/routers/watcher.py")
    assert dossier["line_count"] > 0
    assert "/watcher" in dossier["router_prefixes"]
    assert dossier["references"]["count"] >= 1
    assert dossier["release_recommendation"] in {"modernize_before_alpha", "deprecate_review", "candidate_feature_flag", "default_surface"}
    assert dossier["next_actions"]


def test_functionality_review_queue_lists_remaining_legacy_and_experimental_modules(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(service, "_DB_PATH", tmp_path / "functionality_review.db")
    bootstrap_functionality_review_hints()
    queue = build_functionality_review_queue()

    assert queue["status"] == "warning"
    assert queue["total"] >= 1
    assert any(item["module"] == "auto_memory" for item in queue["items"])
    assert any(item["module"] == "layout_fixer" for item in queue["items"])
    assert queue["next_actions"]


def test_functionality_alpha_config_uses_experimental_modules_as_disabled_set(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(service, "_DB_PATH", tmp_path / "functionality_review.db")
    bootstrap_functionality_review_hints()
    upsert_functionality_review_hint(module="watcher", status="modernize", reason="validated")
    upsert_functionality_review_hint(module="knowledge_tree_api", status="modernize", reason="validated")
    upsert_functionality_review_hint(module="openai_compat", status="experimental", reason="validated")
    upsert_functionality_review_hint(module="auto_memory", status="experimental", reason="validated")
    upsert_functionality_review_hint(module="code_search", status="experimental", reason="validated")
    upsert_functionality_review_hint(module="normalization", status="modernize", reason="validated")
    upsert_functionality_review_hint(module="router_api", status="modernize", reason="validated")
    upsert_functionality_review_hint(module="tree", status="modernize", reason="validated")

    config = build_functionality_alpha_config()

    assert config["status"] == "warning"
    assert "layout_fixer" in config["disabled_modules"]
    assert "auto_memory" in config["disabled_modules"]
    assert "memories" in config["default_surface"]
    assert "openai_compat" in config["disabled_modules_env"]
