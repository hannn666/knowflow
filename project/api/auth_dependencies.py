from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError
from sqlalchemy.orm import Session

from project.api.auth_tokens import decode_access_token
from project.db.business_database import get_database_session
from project.db.business_models import User


_bearer = HTTPBearer(auto_error=False)


def authentication_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Annotated[Session, Depends(get_database_session)],
) -> User:
    if credentials is None:
        raise authentication_error()
    try:
        user_id = decode_access_token(credentials.credentials)
    except InvalidTokenError:
        raise authentication_error() from None
    # A signed token does not make a deleted user valid again.
    user = session.get(User, user_id)
    if user is None:
        raise authentication_error()
    return user
