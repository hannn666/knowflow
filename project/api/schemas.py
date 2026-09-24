from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    session_id: UUID | None = None


class ChatResponse(BaseModel):
    type: Literal["answer", "clarification"]
    message: str
    session_id: UUID
