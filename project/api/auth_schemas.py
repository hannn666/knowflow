from datetime import datetime
from uuid import UUID
from typing import Literal

from pydantic import (
    BaseModel, ConfigDict, EmailStr, Field, SecretStr, field_validator,
)


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr = Field(max_length=320)
    password: SecretStr = Field(min_length=15, max_length=128)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        # KnowFlow account identifiers are case-insensitive.
        return value.lower()


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    created_at: datetime


class LoginRequest(RegisterRequest):
    # Login verifies existing credentials, rather than enforcing signup policy.
    password: SecretStr = Field(min_length=1, max_length=128)


class TokenResponse(BaseModel):
    access_token: str = Field(repr=False)
    refresh_token: str = Field(repr=False)
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    session_id: UUID
    session_expires_at: datetime
    idle_expires_at: datetime


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refresh_token: SecretStr = Field(min_length=43, max_length=43)
