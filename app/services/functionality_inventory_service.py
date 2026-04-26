from __future__ import annotations

import sqlite3
import time
import re
from pathlib import Path
from typing import Any


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_MAIN_PATH = _PROJECT_ROOT / "app" / "main.py"
_DB_PATH = _PROJECT_ROOT / "qdrant_data" / "functionality_review.db"
_STATUS_PRIORITY = {
    "keep": 0,
    "modernize": 1,
    "review_legacy": 2,
    "experimental": 3,
    "review": 4,
}
_HIGH_REVIEW_STATUSES = {"review_legacy", "experimental", "review"}

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS functionality_review_hints (
    scope        TEXT NOT NULL DEFAULT 'supermemory',
    module       TEXT NOT NULL,
    status       TEXT NOT NULL,
    reason       TEXT NOT NULL,
    updated_at   REAL NOT NULL,
    PRIMARY KEY (scope, module)
);
"""

_DEFAULT_REVIEW_HINTS: dict[str, dict[str, str]] = {
    "batch": {"status": "keep", "reason": "Batch write and cleanup remain part of the core memory ingestion surface."},
    "health": {"status": "keep", "reason": "Health and system-info endpoints are required operator-facing trust surfaces."},
    "memories": {"status": "keep", "reason": "Core storage and retrieval surface."},
    "ingest": {"status": "keep", "reason": "Core bootstrap path for bringing knowledge into the system."},
    "outcomes": {"status": "keep", "reason": "Essential feedback loop for memory usefulness."},
    "improvements": {"status": "keep", "reason": "Improvements are now a first-class part of project work-state and audit workflow."},
    "project": {"status": "keep", "reason": "Current project-centric context assembly surface."},
    "project_tasks": {"status": "keep", "reason": "First-class project work-state layer."},
    "skills": {"status": "keep", "reason": "Core reusable knowledge and onboarding surface."},
    "laws": {"status": "keep", "reason": "Governed project rules remain central to the architecture."},
    "docs": {"status": "keep", "reason": "Living docs projection is part of the current product surface."},
    "admin": {"status": "keep", "reason": "Operational visibility, hygiene, and integrity control plane."},
    "mcp_sse": {"status": "keep", "reason": "Primary agent transport and discovery surface."},
    "models": {"status": "keep", "reason": "Registry, coordination, and handoff are still active operational surfaces."},
    "tasks": {"status": "keep", "reason": "Background job polling remains part of the normal operator workflow."},
    "entities": {"status": "modernize", "reason": "Entity-level access is useful but should stay aligned with current knowledge model."},
    "governance": {"status": "modernize", "reason": "Governance remains important, but should be checked against new memory-first workflows."},
    "registry": {"status": "modernize", "reason": "Capability routing is still relevant but should be validated against current model availability logic."},
    "tracker": {"status": "modernize", "reason": "Telemetry/tracking is useful, but should be reviewed against new hygiene boundaries."},
    "learning": {"status": "modernize", "reason": "Learning ledger is active, but needs alignment with hygiene/integrity constraints."},
    "knowledge_tree_api": {"status": "review_legacy", "reason": "Semantic tree-slice API overlaps with newer project context and should be justified explicitly."},
    "layout_fixer": {"status": "experimental", "reason": "Early-stage utility; verify whether it still belongs in the core product."},
    "log_filter": {"status": "experimental", "reason": "Useful niche tool, but not obviously central to current SuperMemory positioning."},
    "watcher": {"status": "review_legacy", "reason": "Historically important, but its current product role vs client-scan/bootstrap needs explicit review."},
    "normalization": {"status": "review_legacy", "reason": "Semantic adaptation layer may still matter, but its place in the main workflow is unclear."},
    "router_api": {"status": "review_legacy", "reason": "Older routing surface should be checked for overlap with current APIs."},
    "code_search": {"status": "review_legacy", "reason": "Potentially useful, but not part of the most recent core workflow."},
    "openai_compat": {"status": "review_legacy", "reason": "Compatibility adapter is valuable only if it still reflects a real supported integration path."},
    "auto_memory": {"status": "review_legacy", "reason": "Earlier automation layer should be checked against current governed knowledge approach."},
    "crystallizer": {"status": "modernize", "reason": "Still strategically important, but should be checked against current skills and governance flows."},
    "dashboard": {"status": "modernize", "reason": "UI surface is useful, but should be reviewed against current operator workflows."},
    "setup": {"status": "keep", "reason": "Bootstrap/setup remains necessary for external instances."},
    "tree": {"status": "review_legacy", "reason": "The full project tree subsystem is rich but no longer obviously part of the core public product story."},
}


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_CREATE_SQL)
    return conn


def list_functionality_review_hints(*, scope: str = "supermemory") -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT scope, module, status, reason, updated_at
            FROM functionality_review_hints
            WHERE scope = ?
            ORDER BY module
            """,
            (scope,),
        ).fetchall()
    return [dict(row) for row in rows]


def upsert_functionality_review_hint(
    *,
    module: str,
    status: str,
    reason: str,
    scope: str = "supermemory",
) -> dict[str, Any]:
    now = time.time()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO functionality_review_hints(scope, module, status, reason, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(scope, module) DO UPDATE SET
                status=excluded.status,
                reason=excluded.reason,
                updated_at=excluded.updated_at
            """,
            (scope, module, status, reason, now),
        )
        conn.commit()
    return {
        "scope": scope,
        "module": module,
        "status": status,
        "reason": reason,
        "updated_at": now,
    }


def bootstrap_functionality_review_hints(
    *,
    scope: str = "supermemory",
    overwrite: bool = False,
) -> dict[str, Any]:
    created = 0
    updated = 0
    skipped = 0
    with _connect() as conn:
        for module, hint in _DEFAULT_REVIEW_HINTS.items():
            existing = conn.execute(
                "SELECT 1 FROM functionality_review_hints WHERE scope = ? AND module = ?",
                (scope, module),
            ).fetchone()
            if existing and not overwrite:
                skipped += 1
                continue
            conn.execute(
                """
                INSERT INTO functionality_review_hints(scope, module, status, reason, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(scope, module) DO UPDATE SET
                    status=excluded.status,
                    reason=excluded.reason,
                    updated_at=excluded.updated_at
                """,
                (scope, module, hint["status"], hint["reason"], time.time()),
            )
            if existing:
                updated += 1
            else:
                created += 1
        conn.commit()
    return {
        "scope": scope,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "total_seeded": len(_DEFAULT_REVIEW_HINTS),
    }


def _classify_surface_kind(line: str) -> str:
    if "app.include_router(" in line and "prefix=prefix" in line:
        return "core"
    if "_try_include(" in line and "prefix, disabled" in line:
        return "optional"
    if 'openai_compat.router' in line:
        return "adapter"
    if 'setup.router' in line:
        return "bootstrap"
    if 'dashboard.router' in line:
        return "ui"
    if 'mcp_sse.router' in line or 'mcp_sse.discovery_router' in line:
        return "transport"
    return "unknown"


def build_functionality_inventory() -> dict[str, Any]:
    text = _MAIN_PATH.read_text(encoding="utf-8")
    items: list[dict[str, Any]] = []
    stored_hints = {
        item["module"]: {"status": item["status"], "reason": item["reason"]}
        for item in list_functionality_review_hints()
    }

    pattern = re.compile(
        r'(?:app\.include_router\((?P<core>[a-zA-Z0-9_]+)\.router|'
        r'app\.include_router\((?P<transport>[a-zA-Z0-9_]+)\.(?:router|discovery_router)|'
        r'_try_include\(app,\s*(?P<optional>[a-zA-Z0-9_]+)\.router,\s*"(?P<name>[a-zA-Z0-9_]+)")'
    )

    seen: set[tuple[str, str]] = set()
    for lineno, line in enumerate(text.splitlines(), 1):
        match = pattern.search(line)
        if not match:
            continue
        module = match.group("name") or match.group("core") or match.group("optional") or match.group("transport")
        if not module:
            continue
        kind = _classify_surface_kind(line)
        key = (module, kind)
        if key in seen:
            continue
        seen.add(key)
        hint = stored_hints.get(module) or _DEFAULT_REVIEW_HINTS.get(
            module,
            {"status": "review", "reason": "No explicit review hint yet."},
        )
        items.append(
            {
                "module": module,
                "surface_kind": kind,
                "status": hint["status"],
                "reason": hint["reason"],
                "source_line": lineno,
            }
        )

    items.sort(key=lambda item: (_STATUS_PRIORITY.get(item["status"], 99), item["module"], item["surface_kind"]))
    by_status: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    for item in items:
        by_status[item["status"]] = by_status.get(item["status"], 0) + 1
        by_kind[item["surface_kind"]] = by_kind.get(item["surface_kind"], 0) + 1

    review_candidates = [item for item in items if item["status"] in _HIGH_REVIEW_STATUSES]
    modernize_candidates = [item for item in items if item["status"] == "modernize"]
    keep_modules = [item["module"] for item in items if item["status"] == "keep"]

    next_actions = [
        "Review modules marked review_legacy or experimental before public release positioning.",
        "Confirm that keep surfaces still match the current product narrative and external-project workflow.",
        "Decide which modules should move behind experimental flags or out of the default product surface.",
    ]

    return {
        "status": "warning" if review_candidates else "ok",
        "inventory_version": 1,
        "total_modules": len(items),
        "by_status": by_status,
        "by_surface_kind": by_kind,
        "summary": {
            "keep_count": len(keep_modules),
            "modernize_count": len(modernize_candidates),
            "review_pressure": len(review_candidates),
            "release_blockers": [item["module"] for item in review_candidates],
        },
        "items": items,
        "next_actions": next_actions,
    }


def build_functionality_release_scope() -> dict[str, Any]:
    inventory = build_functionality_inventory()
    items = inventory["items"]

    default_surface = [
        item["module"]
        for item in items
        if item["status"] == "keep" and item["surface_kind"] in {"core", "optional", "transport", "bootstrap"}
    ]
    modernize_before_alpha = [item["module"] for item in items if item["status"] == "modernize"]
    candidate_feature_flags = [item["module"] for item in items if item["status"] == "experimental"]
    deprecate_review = [item["module"] for item in items if item["status"] == "review_legacy"]

    return {
        "status": "warning" if (candidate_feature_flags or deprecate_review or modernize_before_alpha) else "ok",
        "inventory_version": inventory["inventory_version"],
        "default_surface": default_surface,
        "modernize_before_alpha": modernize_before_alpha,
        "candidate_feature_flags": candidate_feature_flags,
        "deprecate_review": deprecate_review,
        "next_actions": [
            "Freeze the default surface for GitHub alpha around keep modules only.",
            "Decide whether experimental modules ship behind flags or stay out of the default product surface.",
            "Run explicit keep/deprecate decisions for review_legacy modules before public positioning.",
        ],
    }


def _module_file_path(module: str) -> Path | None:
    for candidate in (
        _PROJECT_ROOT / "app" / "routers" / f"{module}.py",
        _PROJECT_ROOT / "app" / "services" / f"{module}.py",
    ):
        if candidate.exists():
            return candidate
    return None


def _router_metadata(text: str) -> dict[str, Any]:
    prefixes = re.findall(r'APIRouter\(\s*prefix="([^"]*)"', text)
    tags_matches = re.findall(r'APIRouter\([^)]*tags=\[([^\]]*)\]', text, flags=re.DOTALL)
    tags: list[str] = []
    for raw in tags_matches:
        tags.extend(re.findall(r'"([^"]+)"', raw))
    return {
        "prefixes": prefixes,
        "tags": sorted(set(tags)),
    }


def _module_references(module: str, module_path: Path | None, limit: int = 20) -> dict[str, Any]:
    samples: list[dict[str, Any]] = []
    total = 0
    pattern = re.compile(rf"\b{re.escape(module)}\b")
    search_roots = (_PROJECT_ROOT / "app", _PROJECT_ROOT / "tests")
    for root in search_roots:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if module_path and path.resolve() == module_path.resolve():
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except Exception:
                continue
            for lineno, line in enumerate(lines, 1):
                if not pattern.search(line):
                    continue
                total += 1
                if len(samples) < limit:
                    try:
                        rel = path.resolve().relative_to(_PROJECT_ROOT).as_posix()
                    except Exception:
                        rel = str(path)
                    samples.append(
                        {
                            "file_path": rel,
                            "line_number": lineno,
                            "snippet": line.strip()[:220],
                        }
                    )
    return {"count": total, "samples": samples}


def build_functionality_review_dossier(module: str) -> dict[str, Any]:
    inventory = build_functionality_inventory()
    item = next((entry for entry in inventory["items"] if entry["module"] == module), None)
    if item is None:
        raise ValueError(f"Unknown module '{module}'")

    module_path = _module_file_path(module)
    metadata = {"prefixes": [], "tags": []}
    line_count = 0
    file_path = ""
    if module_path is not None:
        text = module_path.read_text(encoding="utf-8")
        line_count = len(text.splitlines())
        metadata = _router_metadata(text)
        try:
            file_path = module_path.resolve().relative_to(_PROJECT_ROOT).as_posix()
        except Exception:
            file_path = str(module_path)

    references = _module_references(module, module_path)
    release_scope = build_functionality_release_scope()
    if module in release_scope["default_surface"]:
        release_recommendation = "default_surface"
    elif module in release_scope["modernize_before_alpha"]:
        release_recommendation = "modernize_before_alpha"
    elif module in release_scope["candidate_feature_flags"]:
        release_recommendation = "candidate_feature_flag"
    elif module in release_scope["deprecate_review"]:
        release_recommendation = "deprecate_review"
    else:
        release_recommendation = "unclassified"

    next_actions = {
        "default_surface": ["Keep in default alpha scope and validate docs/onboarding coverage."],
        "modernize_before_alpha": ["Retain the module, but align it with the current product narrative and operator workflow."],
        "candidate_feature_flag": ["Do not keep in the default public surface; ship only behind an explicit flag if retained."],
        "deprecate_review": ["Run an explicit keep/deprecate decision before public release and avoid default-surface exposure."],
        "unclassified": ["Decide whether this module belongs to the public surface at all."],
    }[release_recommendation]

    return {
        "module": module,
        "status": item["status"],
        "reason": item["reason"],
        "surface_kind": item["surface_kind"],
        "source_line": item["source_line"],
        "file_path": file_path,
        "line_count": line_count,
        "router_prefixes": metadata["prefixes"],
        "router_tags": metadata["tags"],
        "references": references,
        "release_recommendation": release_recommendation,
        "next_actions": next_actions,
    }


def build_functionality_review_queue() -> dict[str, Any]:
    inventory = build_functionality_inventory()
    queue = [
        item
        for item in inventory["items"]
        if item["status"] in {"review_legacy", "experimental"}
    ]
    priority_order = {"review_legacy": 0, "experimental": 1}
    queue.sort(key=lambda item: (priority_order.get(item["status"], 99), item["module"]))

    return {
        "status": "warning" if queue else "ok",
        "total": len(queue),
        "items": queue,
        "next_actions": [
            "Resolve review_legacy modules first, because they are closest to deprecate/keep decisions for public alpha.",
            "Treat experimental modules as feature-flag candidates unless they become part of the default product story.",
        ],
    }


def build_functionality_alpha_config() -> dict[str, Any]:
    release_scope = build_functionality_release_scope()
    disabled_modules = sorted(set(release_scope["candidate_feature_flags"]))
    return {
        "status": "warning" if disabled_modules else "ok",
        "inventory_version": release_scope["inventory_version"],
        "default_surface": release_scope["default_surface"],
        "disabled_modules": disabled_modules,
        "disabled_modules_env": ",".join(disabled_modules),
        "next_actions": [
            "Use disabled_modules as the recommended DISABLED_MODULES baseline for public alpha.",
            "Only re-enable experimental modules intentionally and document them as advanced/optional features.",
        ],
    }
