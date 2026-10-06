"""Short-lived access tokens; no refresh tokens or per-token revocation yet."""

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import jwt
from dotenv import load_dotenv


ACCESS_TOKEN_SECONDS = 15 * 60
_ISSUER = "knowflow"
_AUDIENCE = "knowflow-api"
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def get_signing_key() -> str:
    # Lazy loading keeps registration and /health independent of JWT settings.
    load_dotenv(_PROJECT_ROOT / ".env")
    key = os.getenv("KNOWFLOW_JWT_SECRET", "")
    if re.fullmatch(r"[0-9a-fA-F]{64}", key) is None:
        raise RuntimeError("KNOWFLOW_JWT_SECRET must be 64 random hex characters")
    return key


def create_access_token(user_id: UUID) -> str:
    now = int(datetime.now(timezone.utc).timestamp())
    return jwt.encode(
        {"sub": str(user_id), "iat": now, "exp": now + ACCESS_TOKEN_SECONDS,
         "iss": _ISSUER, "aud": _AUDIENCE, "token_use": "access"},
        get_signing_key(), algorithm="HS256",
    )


def decode_access_token(token: str) -> UUID:
    claims = jwt.decode(
        token, get_signing_key(), algorithms=["HS256"],
        issuer=_ISSUER, audience=_AUDIENCE,
        options={"require": ["sub", "iat", "exp", "iss", "aud", "token_use"]},
    )
    if (
        claims["token_use"] != "access"
        or type(claims["iat"]) is not int
        or type(claims["exp"]) is not int
        or not 0 < claims["exp"] - claims["iat"] <= ACCESS_TOKEN_SECONDS
    ):
        raise jwt.InvalidTokenError("Invalid access token claims")
    try:
        return UUID(claims["sub"])
    except (ValueError, TypeError, AttributeError):
        raise jwt.InvalidTokenError("Invalid token subject") from None
