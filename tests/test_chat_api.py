from uuid import UUID

from fastapi.testclient import TestClient

from project.api.app import create_app
from project.api.schemas import ChatRequest, ChatResponse


class FakeChatService:
    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        return ChatResponse(
            type="answer",
            message="测试答案",
            session_id=UUID(
                "550e8400-e29b-41d4-a716-446655440000"
            ),
        )


def test_post_chat_returns_typed_json_response() -> None:
    service = FakeChatService()

    with TestClient(create_app(chat_service=service)) as client:
        response = client.post(
            "/chat",
            json={"message": "什么是混合检索？"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "type": "answer",
        "message": "测试答案",
        "session_id": "550e8400-e29b-41d4-a716-446655440000",
    }
    assert service.requests[0].message == "什么是混合检索？"
    assert service.requests[0].session_id is None


def test_post_chat_rejects_missing_message_before_service() -> None:
    service = FakeChatService()

    with TestClient(create_app(chat_service=service)) as client:
        response = client.post("/chat", json={})

    assert response.status_code == 422
    assert service.requests == []
