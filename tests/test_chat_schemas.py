from uuid import UUID

import pytest
from pydantic import ValidationError

from project.api.schemas import ChatRequest, ChatResponse


def test_chat_request_allows_first_message_without_session_id() -> None:
    request = ChatRequest(message="什么是混合检索？")

    assert request.message == "什么是混合检索？"
    assert request.session_id is None


def test_chat_request_rejects_empty_message() -> None:
    with pytest.raises(ValidationError):
        ChatRequest(message="")


def test_chat_response_serializes_to_json_values() -> None:
    session_id = UUID("550e8400-e29b-41d4-a716-446655440000")

    response = ChatResponse(
        type="answer",
        message="测试答案",
        session_id=session_id,
    )

    assert response.model_dump(mode="json") == {
        "type": "answer",
        "message": "测试答案",
        "session_id": "550e8400-e29b-41d4-a716-446655440000",
    }


def test_chat_response_rejects_unknown_type() -> None:
    with pytest.raises(ValidationError):
        ChatResponse.model_validate(
            {
                "type": "done",
                "message": "测试答案",
                "session_id": "550e8400-e29b-41d4-a716-446655440000",
            }
        )
