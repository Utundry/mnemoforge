from types import SimpleNamespace

import pytest

from scripts import audit_first_run


def test_validate_health_accepts_actionable_degraded_first_run():
    audit_first_run._validate_health(
        {
            "status": "degraded",
            "qdrant": {"reachable": True},
            "llm_providers": {
                "healthy": False,
                "health_rule": "healthy when at least one enabled LLM provider is usable",
            },
        }
    )


def test_validate_health_rejects_unexplained_degradation():
    with pytest.raises(RuntimeError, match="does not explain"):
        audit_first_run._validate_health(
            {
                "status": "degraded",
                "qdrant": {"reachable": True},
                "llm_providers": {"healthy": False},
            }
        )


def test_published_port_parses_loopback_mapping(monkeypatch):
    monkeypatch.setattr(
        audit_first_run,
        "_run",
        lambda cmd: SimpleNamespace(returncode=0, stdout="127.0.0.1:49152\n", stderr=""),
    )

    assert audit_first_run._published_port("first-run") == 49152
