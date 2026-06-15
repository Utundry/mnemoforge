from pathlib import Path

import pytest

from scripts import project_utility


def test_catalog_exposes_repeatable_agent_facing_utilities():
    catalog = project_utility.load_catalog()
    by_id = {item["id"]: item for item in catalog["utilities"]}

    assert {
        "verification.plan",
        "verification.run",
        "verification.baseline",
        "verification.remote_mcp",
        "release.check",
        "release.publish",
        "release.overview",
    } <= set(by_id)
    assert by_id["release.publish"]["confirmation"] == "explicit operator approval"
    assert by_id["verification.run"]["command"][-1] == "scripts\\run_pytest_docker.ps1"
    assert all(item["purpose"] and item["risk"] and item["verification"] for item in by_id.values())


def test_catalog_rejects_duplicate_ids(tmp_path: Path):
    path = tmp_path / "catalog.json"
    path.write_text(
        (
            '{"utilities": ['
            '{"id": "same", "command": ["one"], "title": "one", "category": "test", '
            '"purpose": "one", "parameters": [], "constraints": [], "risk": "one", '
            '"confirmation": "none", "verification": "one"},'
            '{"id": "same", "command": ["two"], "title": "two", "category": "test", '
            '"purpose": "two", "parameters": [], "constraints": [], "risk": "two", '
            '"confirmation": "none", "verification": "two"}]}'
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate"):
        project_utility.load_catalog(path)


def test_command_action_prints_only_executable_commands(monkeypatch, capsys):
    monkeypatch.setattr(
        project_utility.sys,
        "argv",
        ["project_utility.py", "command", "release.check"],
    )

    assert project_utility.main() == 0
    output = capsys.readouterr().out
    assert "scripts.bootstrap_public_release" in output
    assert "scripts.publish_docker_image" in output
    assert "risk:" not in output
