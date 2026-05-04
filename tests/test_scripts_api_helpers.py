from __future__ import annotations

import pytest

import scripts.api_helpers as api_helpers


def test_live_headers_require_api_key(monkeypatch):
    monkeypatch.setattr(api_helpers, "API_KEY", "")

    with pytest.raises(api_helpers.MissingApiKeyError):
        api_helpers.get_live_headers()


def test_live_headers_include_api_key(monkeypatch):
    monkeypatch.setattr(api_helpers, "API_KEY", "test-key")

    assert api_helpers.get_live_headers()["X-API-Key"] == "test-key"


def test_plain_headers_keep_legacy_auth_default(monkeypatch):
    monkeypatch.setattr(api_helpers, "API_KEY", "test-key")

    assert "X-API-Key" not in api_helpers.get_headers()
