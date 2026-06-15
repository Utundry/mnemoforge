from pathlib import Path

from scripts.audit_release_artifacts import (
    FORBIDDEN_CONTEXT_PATHS,
    FORBIDDEN_IMAGE_PATHS,
    _matches_forbidden,
)


ROOT = Path(__file__).resolve().parents[1]


def test_default_production_target_excludes_self_development_assets():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "FROM runtime-base AS self-development" in dockerfile
    assert "FROM runtime-base AS production" in dockerfile
    assert dockerfile.rfind("FROM runtime-base AS production") > dockerfile.rfind(
        "FROM runtime-base AS self-development"
    )
    production = dockerfile.split("FROM runtime-base AS production", 1)[1]
    assert "rm -f docs/PROJECT_LAW.md" in production
    assert ".env.public.example" not in production
    assert "AUTO_BOOTSTRAP_SELF_PROJECT_LAWS=0" in production
    assert "COPY scripts/" not in production


def test_self_development_target_contains_repository_utilities():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    development = dockerfile.split("FROM runtime-base AS self-development", 1)[1].split(
        "FROM runtime-base AS production",
        1,
    )[0]

    assert "COPY scripts/ scripts/" in development
    assert "PROJECT_CAPABILITIES=repository-development-tools" in development


def test_runtime_project_rename_does_not_import_scripts_package():
    source = (ROOT / "app" / "services" / "project_rename_service.py").read_text(encoding="utf-8")

    assert "from app.services.project_identity_migration import migrate_sqlite_file" in source
    assert "from scripts" not in source


def test_release_audit_allows_public_env_example_but_rejects_runtime_env():
    paths = [
        "/app/.env.public.example",
        "/app/.env",
        "/app/.env/private",
    ]

    assert _matches_forbidden(paths, FORBIDDEN_IMAGE_PATHS) == [
        "/app/.env",
        "/app/.env/private",
    ]


def test_release_audit_uses_path_boundaries_for_forbidden_directories():
    paths = [
        "/app/scripts/tool.py",
        "/app/scripts-reference.md",
    ]

    assert _matches_forbidden(paths, FORBIDDEN_IMAGE_PATHS) == [
        "/app/scripts/tool.py",
    ]


def test_release_audit_context_directory_rules_do_not_match_filename_prefixes():
    paths = [
        "system_data/state.db",
        "app/services/system_data_root.py",
    ]

    assert _matches_forbidden(paths, FORBIDDEN_CONTEXT_PATHS) == [
        "system_data/state.db",
    ]
