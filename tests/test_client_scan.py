from __future__ import annotations

import importlib.util
from pathlib import Path
import urllib.error

import pytest


def _load_client_scan():
    path = Path("scripts/client_scan.py").resolve()
    spec = importlib.util.spec_from_file_location("client_scan_mod", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_client_scan_parses_ini_sh_and_hal(tmp_path):
    mod = _load_client_scan()

    ini = tmp_path / "config.ini"
    ini.write_text(
        "[EMC]\nMACHINE = gipric4a_dualz_sim\n# comment\n[HAL]\nHALFILE = core_motion.hal\n",
        encoding="utf-8",
    )
    sh = tmp_path / "start_linuxcnc.sh"
    sh.write_text(
        "#!/bin/bash\n# launch script\nexport INI_FILE=config.ini\nlinuxcnc \"$INI_FILE\"\n",
        encoding="utf-8",
    )
    hal = tmp_path / "core_motion.hal"
    hal.write_text(
        "# motion wiring\nloadrt trivkins\naddf motion-command-handler servo-thread\nnet x-pos-cmd joint.0.motor-pos-cmd\n",
        encoding="utf-8",
    )

    ini_chunks = mod.parse_file(ini, use_llm=False, model=mod.DEFAULT_MODEL)
    sh_chunks = mod.parse_file(sh, use_llm=False, model=mod.DEFAULT_MODEL)
    hal_chunks = mod.parse_file(hal, use_llm=False, model=mod.DEFAULT_MODEL)

    assert len(ini_chunks) == 1
    assert ini_chunks[0]["category"] == "config"
    assert "MACHINE = gipric4a_dualz_sim" in ini_chunks[0]["content"]

    assert len(sh_chunks) == 1
    assert sh_chunks[0]["category"] == "context"
    assert "linuxcnc" in sh_chunks[0]["content"]

    assert len(hal_chunks) == 1
    assert hal_chunks[0]["category"] == "config"
    assert "loadrt trivkins" in hal_chunks[0]["content"]


def test_api_batch_store_retries_on_503_then_succeeds(monkeypatch):
    mod = _load_client_scan()
    calls = {"n": 0}

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"created_ids":["ok-1"],"failed_count":0}'

    def fake_urlopen(req, timeout=30):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(
                url=str(getattr(req, "full_url", "http://test")),
                code=503,
                msg="Service Unavailable",
                hdrs=None,
                fp=None,
            )
        return _FakeResponse()

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(mod.time, "sleep", lambda _: None)

    result = mod.api_batch_store("http://127.0.0.1:8000", [{"content": "x"}])
    assert result["created_ids"] == ["ok-1"]
    assert calls["n"] == 2


def test_api_batch_store_does_not_retry_on_400(monkeypatch):
    mod = _load_client_scan()
    calls = {"n": 0}

    def fake_urlopen(req, timeout=30):
        calls["n"] += 1
        raise urllib.error.HTTPError(
            url=str(getattr(req, "full_url", "http://test")),
            code=400,
            msg="Bad Request",
            hdrs=None,
            fp=None,
        )

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(mod.time, "sleep", lambda _: None)

    with pytest.raises(urllib.error.HTTPError):
        mod.api_batch_store("http://127.0.0.1:8000", [{"content": "x"}])
    assert calls["n"] == 1
