from __future__ import annotations

import re
from collections import defaultdict
from pathlib import PurePath
from typing import Any

from qdrant_client.http import models as qmodels

from app.services.project_knowledge import ProjectKnowledgeService


_GENERIC_GROUP_ALIASES = {
    "root": "project_runtime",
    "py": "python_support",
    "config": "config_support",
}
_BOOTSTRAP_VERSION_NOTE = "Bootstrapped from project-scoped remote client-scan memories."


def _normalise_component_id(value: str) -> str:
    value = value.strip().lower().replace("\\", "/")
    value = re.sub(r"[^a-z0-9/_-]+", "-", value)
    value = value.strip("-_/")
    return value or "root"


def _component_name(component_id: str) -> str:
    return component_id.replace("_", " ").replace("-", " ").replace("/", " / ").title()


def _source_path(row: dict[str, Any]) -> str:
    meta = row.get("meta") or {}
    source_path = str(meta.get("source_path") or "").strip()
    if source_path:
        return source_path
    source = str(row.get("source") or "")
    if source.startswith("client-scan:"):
        return source[len("client-scan:"):].strip()
    return ""


def _infer_root_hint_from_paths(paths: list[str]) -> str:
    normalized = [path.replace("\\", "/").strip() for path in paths if path and path.strip()]
    if not normalized:
        return ""
    dirs = [str(PurePath(path).parent).replace("\\", "/") for path in normalized]
    if not dirs:
        return ""
    root = dirs[0]
    for current in dirs[1:]:
        while root and not current.startswith(root.rstrip("/") + "/") and current != root:
            root = str(PurePath(root).parent).replace("\\", "/")
            if root in (".", ""):
                return ""
    return root.rstrip("/")


def _should_skip_source_path(source_path: str) -> bool:
    if not source_path:
        return True
    name = PurePath(source_path).name.lower()
    if name == "client_scan.py":
        return True
    return False


def _relative_group(source_path: str, root_hint: str) -> str:
    if not source_path:
        return "root"
    source_norm = source_path.replace("\\", "/").rstrip("/")
    root_norm = root_hint.replace("\\", "/").rstrip("/")
    if root_norm and source_norm.startswith(root_norm):
        relative = source_norm[len(root_norm):].lstrip("/")
    else:
        relative = source_norm
    parts = [part for part in PurePath(relative).parts if part not in (".", "")]
    if len(parts) <= 1:
        return "root"
    return str(PurePath(parts[0]))


def _component_identity(group_name: str) -> tuple[str, str]:
    normalized = _normalise_component_id(group_name)
    component_id = _GENERIC_GROUP_ALIASES.get(normalized, normalized)
    return component_id, _component_name(component_id)


def _bootstrap_summary(component_id: str, file_count: int, categories: list[str], file_names: list[str]) -> tuple[str, str]:
    cat_summary = ", ".join(categories[:3]) if categories else "project files"
    file_summary = ", ".join(file_names[:4]) if file_names else "ingested files"
    purpose = (
        f"Bootstrapped component from {file_count} project-scoped ingested files under '{component_id}'."
    )
    implementation = (
        f"Initial component knowledge projected from remote client-scan memories. "
        f"Observed categories: {cat_summary}. Representative files: {file_summary}."
    )
    return purpose, implementation


def _is_bootstrap_component_payload(item: dict[str, Any]) -> bool:
    if str(item.get("bootstrap_origin") or "") == "project_memories":
        return True
    version_note = str(item.get("version_note") or "").strip()
    if version_note == _BOOTSTRAP_VERSION_NOTE:
        return True
    purpose = str(item.get("purpose") or "").strip().lower()
    return purpose.startswith("bootstrapped component from ")


async def bootstrap_components_from_project_memories(
    *,
    project_id: str,
    root_hint: str,
    qdrant,
    ollama,
    limit: int = 1000,
) -> dict[str, Any]:
    results, _ = await qdrant._client.scroll(
        collection_name=qdrant._collection,
        scroll_filter=qmodels.Filter(
            must=[
                qmodels.FieldCondition(key="project", match=qmodels.MatchValue(value=project_id)),
            ]
        ),
        limit=limit,
        with_payload=True,
        with_vectors=False,
    )
    rows = [r.payload for r in results if r.payload]
    rows = [
        row for row in rows
        if str(row.get("source") or "").startswith("client-scan:")
    ]
    rows = [
        row for row in rows
        if not _should_skip_source_path(_source_path(row))
    ]

    if not rows:
        return {
            "project_id": project_id,
            "scanned_memories": 0,
            "created_components": 0,
            "components": [],
            "message": "No project-scoped client-scan memories found for bootstrap.",
        }

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[_relative_group(_source_path(row), root_hint)].append(row)

    svc = ProjectKnowledgeService(qdrant._client, ollama)
    await svc.ensure_collection()
    existing_components = await svc.list_components(project_id)
    existing_bootstrap_components = {
        str(item.get("component_id") or "")
        for item in existing_components
        if _is_bootstrap_component_payload(item)
    }

    created_components: list[str] = []
    for group_name, items in grouped.items():
        source_paths = sorted({_source_path(item) for item in items if _source_path(item)})
        source_paths = [path for path in source_paths if not _should_skip_source_path(path)]
        if not source_paths:
            continue
        contents = [str(item.get("content") or "") for item in items if str(item.get("content") or "").strip()]
        file_hash = svc.compute_hash(contents) if contents else svc.compute_hash(source_paths)
        file_names = [PurePath(path).name for path in source_paths]
        categories = sorted({str(item.get("category") or "") for item in items if str(item.get("category") or "")})
        component_id, component_name = _component_identity(group_name)
        purpose, implementation = _bootstrap_summary(component_id, len(source_paths), categories, file_names)
        snapshot = None
        for item in items:
            candidate = item.get("snapshot")
            if isinstance(candidate, dict) and candidate:
                snapshot = candidate
                break
        await svc.upsert_component(
            project_id=project_id,
            component_id=component_id,
            name=component_name,
            purpose=purpose,
            implementation=implementation,
            key_files=source_paths[:25],
            endpoints=[],
            status="wip",
            file_hash=file_hash,
            version_note=_BOOTSTRAP_VERSION_NOTE,
            snapshot=snapshot,
            extra_payload={
                "bootstrap_origin": "project_memories",
                "bootstrap_root_hint": root_hint,
                "bootstrap_group": group_name,
            },
        )
        created_components.append(component_id)

    obsolete_components = sorted(existing_bootstrap_components - set(created_components))
    for component_id in obsolete_components:
        await svc.delete_component(project_id, component_id)

    return {
        "project_id": project_id,
        "scanned_memories": len(rows),
        "created_components": len(created_components),
        "components": created_components,
        "message": "",
        "removed_components": obsolete_components,
    }


async def infer_project_root_hint_from_memories(
    *,
    project_id: str,
    qdrant,
    limit: int = 1000,
) -> str:
    results, _ = await qdrant._client.scroll(
        collection_name=qdrant._collection,
        scroll_filter=qmodels.Filter(
            must=[qmodels.FieldCondition(key="project", match=qmodels.MatchValue(value=project_id))]
        ),
        limit=limit,
        with_payload=True,
        with_vectors=False,
    )
    rows = [r.payload for r in results if r.payload]
    source_paths = [
        _source_path(row)
        for row in rows
        if str(row.get("source") or "").startswith("client-scan:")
        and not _should_skip_source_path(_source_path(row))
    ]
    return _infer_root_hint_from_paths(source_paths)
