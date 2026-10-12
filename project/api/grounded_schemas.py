"""Public version QA contracts; clients never choose source/model/backend settings."""
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=1000, strict=True)

    @field_validator("question")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("Question must not be blank")
        try:
            if len(value.encode("utf-8"))>2000:
                raise ValueError("Question exceeds UTF-8 byte budget")
        except UnicodeError:
            raise ValueError("Question must contain valid Unicode") from None
        return value.strip()


class Citation(BaseModel):
    knowledge_base_id: UUID
    document_id: UUID
    document_version_id: UUID
    original_filename: str
    page_number: int
    chunk_id: UUID
    parent_id: UUID
    evidence_text: str


class AskResponse(BaseModel):
    status: Literal["answered", "insufficient_evidence"]
    answer: str
    citations: list[Citation]


class IndexResponse(BaseModel):
    knowledge_base_id: UUID
    document_id: UUID
    document_version_id: UUID
    index_status: Literal["verified"] = "verified"
    point_count: int
    parent_count: int
    parse_attempt_id: UUID
    parse_sha256: str
    chunking_fingerprint: str
    embedding_fingerprint: str
