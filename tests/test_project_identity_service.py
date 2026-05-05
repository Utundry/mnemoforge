from pathlib import Path

from app.services.project_identity_service import ProjectIdentityStore


def test_project_identity_store_resolves_aliases(tmp_path: Path):
    store = ProjectIdentityStore(tmp_path / "project_identity.db")
    try:
        store.upsert_alias(alias="mnemoforge", project_id="mnemoforge", reason="canonical")
        store.upsert_alias(alias="supermemory", project_id="mnemoforge", reason="old working name")

        assert store.resolve("supermemory") == "mnemoforge"
        assert store.resolve("mnemoforge") == "mnemoforge"
        assert store.resolve("other") == "other"
        assert store.aliases_for("mnemoforge") == ["mnemoforge", "supermemory"]
    finally:
        store.close()
