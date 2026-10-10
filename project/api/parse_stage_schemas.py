from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class ParseStageResponse(BaseModel):
    knowledge_base_id: UUID
    document_id: UUID
    document_version_id: UUID
    original_filename: str
    parse_status: Literal['not_started', 'running', 'succeeded', 'failed']
    attempt_id: UUID | None = None
    page_count: int | None = None
    has_text: bool | None = None
    error_code: str | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
