from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError

from project.api.auth_tokens import AccessIdentity, decode_access_identity
from project.api.auth_session_service import InvalidSession, check_session
from project.db.business_database import get_database_session
from project.db.business_models import User


_bearer = HTTPBearer(auto_error=False)


def authentication_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired credentials",
        headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
    )


def get_current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Annotated[Session, Depends(get_database_session)],
) -> User:
    if credentials is None:
        raise authentication_error()
    try:
        identity = decode_access_identity(credentials.credentials)
        check_session(session, identity, activity=request.url.path.rstrip("/") != "/auth/me")
        user = session.get(User, identity.user_id)
    except (InvalidTokenError, InvalidSession):
        raise authentication_error() from None
    except (SQLAlchemyError, RuntimeError):
        session.rollback()
        raise HTTPException(503, "Authentication service unavailable",
                            headers={"Cache-Control": "no-store"}) from None
    # A signed token does not make a deleted user valid again.
    if user is None:
        raise authentication_error()
    request.state.auth_identity = identity
    return user


def get_current_identity(
    request: Request, user: Annotated[User, Depends(get_current_user)],
) -> AccessIdentity:
    """Immutable context captured only AFTER JWT, session and user validation."""
    identity = request.state.auth_identity
    if identity.user_id != user.id:
        raise authentication_error()
    return identity
