from fastapi import FastAPI
from pydantic import BaseModel

from project.api.chat_service import ChatService, LangGraphChatService
from project.api.schemas import ChatRequest, ChatResponse


class HealthResponse(BaseModel):
    status: str


def create_app(
    chat_service: ChatService | None = None,
) -> FastAPI:
    app = FastAPI()
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
