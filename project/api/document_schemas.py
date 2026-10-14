from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class DocumentVersionResponse(BaseModel):
    knowledge_base_id: UUID
    document_id: UUID
    document_version_id: UUID
    original_filename: str
    status: str
    created_at: datetime


class DocumentListItemResponse(BaseModel):
    id: UUID
    knowledge_base_id: UUID
    created_at: datetime
    latest_version: DocumentVersionResponse | None
