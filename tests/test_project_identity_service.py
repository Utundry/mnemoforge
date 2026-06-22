import sqlite3
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


def test_project_identity_store_resolves_transitive_rename_aliases():
    original_connect = pis.sqlite3.connect
    pis.sqlite3.connect = lambda *args, **kwargs: original_connect(":memory:", check_same_thread=False)
    try:
        store = ProjectIdentityStore(Path("qdrant_data") / "project_identity_test.db")
        store.upsert_alias(alias="mnemoforge", project_id="mnemoforge", reason="canonical")
        store.upsert_alias(alias="supermemory", project_id="mnemoforge", reason="old working name")
        store._conn.execute(
            """
            INSERT INTO project_identity_aliases (alias, project_id, status, reason, created_at, updated_at)
            VALUES ('sloplesscode', 'supermemory', 'active', 'public rename', 1, 1)
            """
        )
        store._conn.commit()

        assert store.resolve("sloplesscode") == "mnemoforge"
        assert store.resolve("supermemory") == "mnemoforge"
        assert store.aliases_for("sloplesscode") == ["mnemoforge", "sloplesscode", "supermemory"]

        aliases = store.list_aliases("sloplesscode")
        assert {item["alias"] for item in aliases} == {"mnemoforge", "sloplesscode", "supermemory"}
        assert {store.resolve(item["project_id"]) for item in aliases} == {"mnemoforge"}
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

def test_project_identity_store_migrates_effective_dates(tmp_path: Path):
    db_path = tmp_path / "project_identity.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE project_identity_aliases (
            alias TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            reason TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO project_identity_aliases (alias, project_id, status, reason, created_at, updated_at)
        VALUES ('supermemory', 'mnemoforge', 'active', 'old working name', 123.0, 124.0)
        """
    )
    conn.commit()
    conn.close()

    store = ProjectIdentityStore(db_path)
    try:
        aliases = store.list_aliases("mnemoforge")
        migrated = next(item for item in aliases if item["alias"] == "supermemory")
        assert migrated["effective_from"] == 123.0
        assert migrated["effective_to"] is None

        created = store.upsert_alias(
            alias="sloplesscode",
            project_id="mnemoforge",
            reason="public rename",
            effective_from=456.0,
            effective_to=789.0,
        )
        assert created["effective_from"] == 456.0
        assert created["effective_to"] == 789.0
        listed = next(item for item in store.list_aliases("mnemoforge") if item["alias"] == "sloplesscode")
        assert listed["effective_from"] == 456.0
        assert listed["effective_to"] == 789.0
    finally:
        store.close()
