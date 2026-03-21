from datetime import datetime
from pydantic import BaseModel, Field


class DocsSection(BaseModel):
    name: str
    content: str


class DocsStatus(BaseModel):
    project: str
    generated_at: datetime
    sections: dict[str, DocsSection]


class DocsRebuildRequest(BaseModel):
    project: str = Field(
        default="supermemory",
        min_length=1,
        max_length=128,
        description="Project identifier used for docs cache keying.",
    )
    force: bool = Field(default=False)
