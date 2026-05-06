from __future__ import annotations

from pathlib import Path

from app.services.code_hardcoding_audit_service import run_code_hardcoding_audit


def test_code_hardcoding_audit_detects_private_urls_keys_and_scope_ids(tmp_path: Path):
    src = tmp_path / "app"
    src.mkdir()
    sample = src / "sample.py"
    sample.write_text(
        "\n".join(
            [
                'DEFAULT_SERVER = "http://192.168.1.138:8000"',
                'DEFAULT_KEY = "mnemoforge-local"',
                'slice_id = "qdrant.skill_domain_tags_filter"',
                r'WINDOWS_PATH = "C:\\Users\\User\\secret.txt"',
            ]
        ),
        encoding="utf-8",
    )

    result = run_code_hardcoding_audit(repo_root=tmp_path, roots=("app",), limit_per_category=20)

    assert result["status"] == "warning"
    categories = {item["category"] for item in result["findings"]}
    assert "private_network_url" in categories
    assert "hardcoded_api_key_value" in categories
    assert "hardcoded_scope_identifier" in categories
    assert "hardcoded_local_path" in categories
    assert result["next_actions"]


def test_code_hardcoding_audit_suppresses_placeholder_api_key_and_self_file(tmp_path: Path):
    app_dir = tmp_path / "app" / "services"
    app_dir.mkdir(parents=True)
    audit_file = app_dir / "code_hardcoding_audit_service.py"
    audit_file.write_text('PATTERN = "__API_KEY__"\n', encoding="utf-8")

    other_dir = tmp_path / "app"
    sample = other_dir / "placeholder.py"
    sample.write_text('API_KEY = "__API_KEY__"\n', encoding="utf-8")

    result = run_code_hardcoding_audit(repo_root=tmp_path, roots=("app",), limit_per_category=20)

    assert result["total_findings"] == 0


def test_code_hardcoding_audit_suppresses_example_comments_and_system_identifier_constants(tmp_path: Path):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    sample = app_dir / "sample.py"
    sample.write_text(
        "\n".join(
            [
                '# Example: http://192.168.1.138:8000',
                '# Example: /home/user/projects,/tmp',
                'SKILL_DOMAIN_TAGS_FILTER_SLICE_ID = "qdrant.skill_domain_tags_filter"',
            ]
        ),
        encoding="utf-8",
    )

    result = run_code_hardcoding_audit(repo_root=tmp_path, roots=("app",), limit_per_category=20)

    assert result["total_findings"] == 0
