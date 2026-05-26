from pathlib import Path

from app.services import project_identity_service as pis
from app.services.project_identity_service import ProjectIdentityStore


def test_project_identity_store_resolves_aliases():
    original_connect = pis.sqlite3.connect
    pis.sqlite3.connect = lambda *args, **kwargs: original_connect(":memory:", check_same_thread=False)
    try:
        store = ProjectIdentityStore(Path("qdrant_data") / "project_identity_test.db")
        store.upsert_alias(alias="mnemoforge", project_id="mnemoforge", reason="canonical")
        store.upsert_alias(alias="supermemory", project_id="mnemoforge", reason="old working name")

        assert store.resolve("supermemory") == "mnemoforge"
        assert store.resolve("mnemoforge") == "mnemoforge"
        assert store.resolve("other") == "other"
        assert store.aliases_for("mnemoforge") == ["mnemoforge", "supermemory"]
    finally:
        store.close()
        pis.sqlite3.connect = original_connect


def test_project_identity_store_seeds_public_alias(monkeypatch):
    original_connect = pis.sqlite3.connect
    pis.sqlite3.connect = lambda *args, **kwargs: original_connect(":memory:", check_same_thread=False)
    try:
        monkeypatch.setattr(pis.settings, "self_project_id", "mnemoforge", raising=False)
        monkeypatch.setattr(pis.settings, "public_project_alias", "sloplesscode", raising=False)
        store = ProjectIdentityStore(Path("qdrant_data") / "project_identity_test.db")
        monkeypatch.setattr(pis, "_STORE", None, raising=False)
        monkeypatch.setattr(pis, "ProjectIdentityStore", lambda *_args, **_kwargs: store)

        resolved = pis.get_project_identity_store()

        assert resolved is store
        assert store.resolve("sloplesscode") == "mnemoforge"
        assert "sloplesscode" in store.aliases_for("mnemoforge")
    finally:
        store.close()
        pis.sqlite3.connect = original_connect
