from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session

from project.api.auth_dependencies import get_current_user
from project.api.auth_schemas import LoginRequest, RefreshRequest, RegisterRequest, TokenResponse, UserResponse
from project.api.auth_service import EmailAlreadyRegistered, authenticate_user, register_user
from project.api.auth_session_service import InvalidSession, revoke_session, rotate_refresh, start_session
from project.db.business_database import get_database_session
from project.db.business_models import User


PRIVATE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


class AuthRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handle(request: Request):
            try:
                response = await original(request)
            except HTTPException as error:
                error.headers = {**(error.headers or {}), **PRIVATE}
                raise
            except RequestValidationError:
                raise  # App handler strips submitted values, including RTs.
            except Exception:
                return JSONResponse({"detail": "Authentication service unavailable"},
                                    status_code=503, headers=PRIVATE)
            response.headers.update(PRIVATE)
            return response
        return handle


router = APIRouter(prefix="/auth", tags=["auth"], route_class=AuthRoute)


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
    return start_session(session, user.id)


@router.post("/refresh", response_model=TokenResponse)
def refresh(request: RefreshRequest,
            session: Annotated[Session, Depends(get_database_session)]) -> TokenResponse:
    try:
        return rotate_refresh(session, request.refresh_token.get_secret_value())
    except InvalidSession:
        raise HTTPException(401, "Invalid or expired credentials",
                            headers={"WWW-Authenticate": "Bearer"}) from None


@router.post("/logout", status_code=204)
def logout(request: RefreshRequest,
           session: Annotated[Session, Depends(get_database_session)]) -> Response:
    try:
        revoke_session(session, request.refresh_token.get_secret_value())
    except InvalidSession:
        raise HTTPException(401, "Invalid or expired credentials",
                            headers={"WWW-Authenticate": "Bearer"}) from None
    return Response(status_code=204, headers=PRIVATE)


@router.get("/me", response_model=UserResponse)
def me(
    response: Response,
    user: Annotated[User, Depends(get_current_user)],
) -> UserResponse:
    response.headers["Cache-Control"] = "no-store"
    return UserResponse.model_validate(user)
