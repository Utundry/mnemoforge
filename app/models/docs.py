from datetime import datetime
from pydantic import BaseModel, Field


class DocsSection(BaseModel):
    name: str
    content: str


class DocsStatus(BaseModel):
    project: str
    generated_at: datetime
    sections: dict[str, DocsSection]
    snapshot: dict = Field(default_factory=dict)
    last_rebuild_mode: str | None = None
    stale: bool = False
    stale_reason: str | None = None
    candidate_generated_at: datetime | None = None
    candidate_sections: dict[str, DocsSection] = Field(default_factory=dict)
    last_review_action: str | None = None
    last_reviewed_by: str | None = None
    last_review_source: str | None = None
    last_reviewed_at: datetime | None = None
    last_review_reason: str | None = None


class DocsRebuildRequest(BaseModel):
    project: str = Field(
        default="mnemoforge",
        min_length=1,
        max_length=128,
        description="Project identifier used for docs cache keying.",
    )
    force: bool = Field(default=False)
    changed_component_ids: list[str] = Field(default_factory=list, max_length=200)
    changed_files: list[str] = Field(default_factory=list, max_length=500)


class DocsCandidateReviewRequest(BaseModel):
    reviewed_by: str = Field("user", min_length=1, max_length=256)
    review_source: str = Field("inline_user_approval", max_length=128)
    reason: str = Field("", max_length=1000)
