from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from project.api.chat_service import ChatService, LangGraphChatService
from project.api.auth_routes import router as auth_router
from project.api.schemas import ChatRequest, ChatResponse


class HealthResponse(BaseModel):
    status: str


def create_app(
    chat_service: ChatService | None = None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(auth_router)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        if request.url.path.rstrip("/") == "/auth/register":
            # Default validation errors may echo plaintext passwords in input.
            return JSONResponse(
                status_code=422,
                content={"detail": [
                    {key: item[key] for key in ("type", "loc", "msg")}
                    for item in error.errors()
                ]},
            )
        return await request_validation_exception_handler(request, error)

    service = (
        chat_service
        if chat_service is not None
        else LangGraphChatService()
    )

    @app.get("/health")
    def get_health() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.post("/chat", response_model=ChatResponse)
    def post_chat(request: ChatRequest) -> ChatResponse:
        return service.chat(request)

    return app
