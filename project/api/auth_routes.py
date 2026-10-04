from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from project.api.auth_schemas import RegisterRequest, UserResponse
from project.api.auth_service import EmailAlreadyRegistered, register_user
from project.db.business_database import get_database_session


router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED
)
def register(
    request: RegisterRequest,
    session: Annotated[Session, Depends(get_database_session)],
) -> UserResponse:
    try:
        user = register_user(
            session, request.email, request.password.get_secret_value()
        )
    except EmailAlreadyRegistered:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered",
        ) from None
    return UserResponse.model_validate(user)
