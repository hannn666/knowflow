from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from project.api.auth_dependencies import get_current_user
from project.api.auth_schemas import LoginRequest, RegisterRequest, TokenResponse, UserResponse
from project.api.auth_service import EmailAlreadyRegistered, authenticate_user, register_user
from project.api.auth_tokens import ACCESS_TOKEN_SECONDS, create_access_token
from project.db.business_database import get_database_session
from project.db.business_models import User


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


@router.post("/login", response_model=TokenResponse)
def login(
    request: LoginRequest,
    response: Response,
    session: Annotated[Session, Depends(get_database_session)],
) -> TokenResponse:
    user = authenticate_user(session, request.email, request.password.get_secret_value())
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return TokenResponse(
        access_token=create_access_token(user.id), expires_in=ACCESS_TOKEN_SECONDS
    )


@router.get("/me", response_model=UserResponse)
def me(
    response: Response,
    user: Annotated[User, Depends(get_current_user)],
) -> UserResponse:
    response.headers["Cache-Control"] = "no-store"
    return UserResponse.model_validate(user)
