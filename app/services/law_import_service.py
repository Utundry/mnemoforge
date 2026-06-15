from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.core.path_security import check_path_allowed
from app.models.law import (
    ProjectLawCreate,
    ProjectLawImportResponse,
    ProjectLawUpdate,
)
from app.services.law_service import (
    CONFIRMED_STATUSES,
    create_project_law,
    list_project_laws,
    update_project_law,
)


_LAW_HEADING_RE = re.compile(r"^##\s+Law\s+\d+\s*:\s*(.+?)\s*$", re.MULTILINE)
_WS_RE = re.compile(r"[ \t]+")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass(slots=True)
class ParsedLawDraft:
    title: str
    statement: str
    rationale: str
    evidence: list[str]
    topic_path: str
    tags: list[str]


def _normalize_paragraph(text: str) -> str:
    lines = [line.strip() for line in text.strip().splitlines()]
    chunks: list[str] = []
    for line in lines:
        if not line:
            continue
        if line.startswith("- "):
            chunks.append(line)
        else:
            chunks.append(_WS_RE.sub(" ", line))
    return "\n".join(chunks).strip()


def _slug(value: str) -> str:
    return _SLUG_RE.sub("-", value.strip().lower()).strip("-") or "law"


def parse_project_laws_markdown(markdown: str, *, source_path: str) -> list[ParsedLawDraft]:
    drafts: list[ParsedLawDraft] = []
    matches = list(_LAW_HEADING_RE.finditer(markdown))
    for index, match in enumerate(matches):
        title = match.group(1).strip()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        section_body = markdown[start:end].strip()
        if not section_body:
            continue
        paragraphs = [
            _normalize_paragraph(chunk)
            for chunk in re.split(r"\n\s*\n", section_body)
            if chunk.strip()
        ]
        if not paragraphs:
            continue
        capability_tags = [
            f"requires-capability:{paragraph.split(':', 1)[1].strip()}"
            for paragraph in paragraphs
            if paragraph.casefold().startswith("requires capability:")
            and paragraph.split(":", 1)[1].strip()
        ]
        content_paragraphs = [
            paragraph
            for paragraph in paragraphs
            if not paragraph.casefold().startswith("requires capability:")
        ]
        if not content_paragraphs:
            continue
        statement = content_paragraphs[0]
        rationale = "\n\n".join(content_paragraphs[1:]).strip()
        drafts.append(
            ParsedLawDraft(
                title=title,
                statement=statement,
                rationale=rationale,
                evidence=[
                    f"Imported from {source_path}",
                    f"Section: {title}",
                ],
                topic_path=f"laws/{_slug(title)}",
                tags=capability_tags,
            )
        )
    return drafts


def load_project_laws_markdown(path: str) -> str:
    source = Path(path)
    check_path_allowed(source)
    return source.read_text(encoding="utf-8", errors="replace")


async def import_project_laws_from_markdown(
    *,
    qdrant,
    ollama,
    project: str,
    path: str,
    agent_id: str,
    confirmed_by: str,
    confirmation_source: str,
    reason: str,
    extra_tags: list[str] | None = None,
) -> ProjectLawImportResponse:
    markdown = load_project_laws_markdown(path)
    drafts = parse_project_laws_markdown(markdown, source_path=path)
    existing = await list_project_laws(
        qdrant,
        project=project,
        status="all",
        include_promoted=False,
        limit=200,
    )
    existing_by_title = {law.title: law for law in existing if law.scope == "project"}

    created_ids: list[str] = []
    staged_ids: list[str] = []
    created = 0
    skipped_existing = 0
    staged_candidate_revision = 0

    for draft in drafts:
        existing_law = existing_by_title.get(draft.title)
        if existing_law is None:
            created_record = await create_project_law(
                qdrant,
                ollama,
                ProjectLawCreate(
                    project=project,
                    title=draft.title,
                    statement=draft.statement,
                    rationale=draft.rationale,
                    evidence=draft.evidence,
                    agent_id=agent_id,
                    scope="project",
                    status="active",
                    topic_path=draft.topic_path,
                    tags=["imported", "project_law_markdown", *draft.tags, *(extra_tags or [])],
                    confirmed_by=confirmed_by,
                    confirmation_source=confirmation_source,
                ),
            )
            created += 1
            created_ids.append(created_record.id)
            continue

        if (
            existing_law.statement == draft.statement
            and existing_law.rationale == draft.rationale
            and existing_law.topic_path == draft.topic_path
        ):
            skipped_existing += 1
            continue

        updated = await update_project_law(
            qdrant,
            ollama,
            existing_law.id,
            ProjectLawUpdate(
                statement=draft.statement,
                rationale=draft.rationale,
                evidence=draft.evidence,
                topic_path=draft.topic_path,
                tags=["imported", "project_law_markdown", *draft.tags, *(extra_tags or [])],
            ),
        )
        if existing_law.status in CONFIRMED_STATUSES and updated.candidate_revision is not None:
            staged_candidate_revision += 1
            staged_ids.append(updated.id)
        else:
            skipped_existing += 1

    return ProjectLawImportResponse(
        project=project,
        source_path=path,
        parsed=len(drafts),
        created=created,
        skipped_existing=skipped_existing,
        staged_candidate_revision=staged_candidate_revision,
        created_ids=created_ids,
        staged_ids=staged_ids,
    )


async def ensure_project_laws_from_markdown_if_missing(
    *,
    qdrant,
    ollama,
    project: str,
    path: str,
    agent_id: str,
    confirmed_by: str,
    confirmation_source: str,
    reason: str,
    extra_tags: list[str] | None = None,
) -> ProjectLawImportResponse | None:
    """
    Import project laws from markdown only when the project currently has no active laws.

    Returns:
      - ProjectLawImportResponse when bootstrap import is executed
      - None when source file is missing or active laws already exist
    """
    source = Path(path)
    if not source.exists():
        return None

    active = await list_project_laws(
        qdrant,
        project=project,
        status="active",
        include_promoted=False,
        limit=1,
    )
    if active:
        return None

    return await import_project_laws_from_markdown(
        qdrant=qdrant,
        ollama=ollama,
        project=project,
        path=path,
        agent_id=agent_id,
        confirmed_by=confirmed_by,
        confirmation_source=confirmation_source,
        reason=reason,
        extra_tags=extra_tags,
    )
