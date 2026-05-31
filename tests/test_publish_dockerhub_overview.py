from __future__ import annotations

from types import SimpleNamespace

from scripts import publish_dockerhub_overview as helper


def test_resolve_repository_rejects_tagged_value():
    try:
        helper._resolve_repository("caveboy/sloplesscode:latest")
    except ValueError as exc:
        assert "tag" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_publish_overview_dry_run_reports_payload_size():
    result = helper.publish_overview(
        repository="caveboy/sloplesscode",
        overview="# SloplessCode\n",
        description="Operational continuity infrastructure for AI coding agents.",
        dry_run=True,
    )

    assert result == {
        "repository": "caveboy/sloplesscode",
        "description": "Operational continuity infrastructure for AI coding agents.",
        "full_description_length": len("# SloplessCode\n"),
        "dry_run": True,
    }


def test_load_docker_credential_uses_configured_helper(monkeypatch, tmp_path):
    docker_config = tmp_path / ".docker"
    docker_config.mkdir()
    (docker_config / "config.json").write_text('{"auths": {}, "credsStore": "desktop"}', encoding="utf-8")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"Username":"user","Secret":"secret"}', stderr="")

    monkeypatch.setenv("DOCKER_CONFIG", str(docker_config))
    monkeypatch.setattr(helper.subprocess, "run", fake_run)

    assert helper._load_docker_credential() == ("user", "secret")
    assert calls[0][0] == ["docker-credential-desktop", "get"]
    assert calls[0][1]["input"] == helper.DEFAULT_CREDENTIAL_SERVER


def test_login_posts_username_and_token(monkeypatch):
    seen = {}

    def fake_request_json(url, *, method="GET", token="", payload=None):
        seen.update({"url": url, "method": method, "token": token, "payload": payload})
        return {"token": "jwt-token"}

    monkeypatch.setattr(helper, "_request_json", fake_request_json)

    assert helper._login(("user", "pat")) == "jwt-token"
    assert seen == {
        "url": helper.DEFAULT_LOGIN_URL,
        "method": "POST",
        "token": "",
        "payload": {"username": "user", "password": "pat"},
    }


def test_publish_overview_patches_repository(monkeypatch):
    seen = []

    monkeypatch.setattr(helper, "_resolve_auth", lambda: "jwt-token")

    def fake_request_json(url, *, method="GET", token="", payload=None):
        seen.append((url, method, token, payload))
        return {"ok": True}

    monkeypatch.setattr(helper, "_request_json", fake_request_json)

    result = helper.publish_overview(
        repository="caveboy/sloplesscode",
        overview="# SloplessCode\n",
        description="Operational continuity infrastructure for AI coding agents.",
    )

    assert result == {"ok": True}
    assert seen == [
        (
            "https://hub.docker.com/v2/repositories/caveboy/sloplesscode/",
            "PATCH",
            "jwt-token",
            {
                "description": "Operational continuity infrastructure for AI coding agents.",
                "full_description": "# SloplessCode\n",
            },
        )
    ]
