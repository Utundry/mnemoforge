from pathlib import Path

from app.services import system_data_root as sdr


def test_system_data_root_defaults_to_legacy_qdrant_data(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SLOPLESSCODE_DATA_DIR", raising=False)
    monkeypatch.delenv("MNEMOFORGE_DATA_DIR", raising=False)

    assert sdr.get_system_data_root(create=False) == Path("qdrant_data")


def test_system_data_root_prefers_existing_canonical_dir(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SLOPLESSCODE_DATA_DIR", raising=False)
    monkeypatch.delenv("MNEMOFORGE_DATA_DIR", raising=False)
    Path("system_data").mkdir()

    assert sdr.get_system_data_root(create=False) == Path("system_data")


def test_system_data_root_prefers_sloplesscode_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("SLOPLESSCODE_DATA_DIR", str(tmp_path / "new-root"))
    monkeypatch.setenv("MNEMOFORGE_DATA_DIR", str(tmp_path / "legacy-root"))

    assert sdr.get_system_data_root(create=False) == tmp_path / "new-root"
    assert sdr.data_path("store.db") == tmp_path / "new-root" / "store.db"


def test_system_data_root_keeps_legacy_env_compatibility(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("SLOPLESSCODE_DATA_DIR", raising=False)
    monkeypatch.setenv("MNEMOFORGE_DATA_DIR", str(tmp_path / "legacy-root"))

    info = sdr.describe_system_data_root()

    assert info["root"] == str(tmp_path / "legacy-root")
    assert info["explicit_env"] == "MNEMOFORGE_DATA_DIR"
