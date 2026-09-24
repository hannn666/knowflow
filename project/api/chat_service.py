from __future__ import annotations

import sys
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Literal, Protocol
from uuid import uuid4

from langchain_core.messages import HumanMessage

from project.api.schemas import ChatRequest, ChatResponse


class RAGSystemLike(Protocol):
    agent_graph: Any | None

    def get_config(
        self,
        thread_id: str | None = None,
    ) -> dict[str, Any]: ...


RAGSystemFactory = Callable[[], RAGSystemLike]


class ChatService(Protocol):
    def chat(self, request: ChatRequest) -> ChatResponse: ...


def _create_rag_system() -> RAGSystemLike:
    project_directory = str(Path(__file__).resolve().parents[1])
    if project_directory not in sys.path:
        sys.path.insert(0, project_directory)

    from core.rag_system import RAGSystem

    rag_system = RAGSystem()
    rag_system.initialize()
    return rag_system


class LangGraphChatService:
    def __init__(
        self,
        rag_system_factory: RAGSystemFactory = _create_rag_system,
    ) -> None:
        self._rag_system_factory = rag_system_factory
        self._rag_system: RAGSystemLike | None = None
        self._initialization_lock = Lock()

    def _get_rag_system(self) -> RAGSystemLike:
        if self._rag_system is None:
            with self._initialization_lock:
                if self._rag_system is None:
                    self._rag_system = self._rag_system_factory()

        return self._rag_system

    def chat(self, request: ChatRequest) -> ChatResponse:
        session_id = request.session_id or uuid4()
        rag_system = self._get_rag_system()
        graph = rag_system.agent_graph

        if graph is None:
            raise RuntimeError("RAG system was not initialized.")

        config = rag_system.get_config(str(session_id))
        current_state = graph.get_state(config)
        user_message = HumanMessage(content=request.message)

        if current_state.next:
            graph.update_state(config, {"messages": [user_message]})
            graph.invoke(None, config=config)
        else:
            graph.invoke({"messages": [user_message]}, config=config)

        final_state = graph.get_state(config)
        messages = final_state.values.get("messages", [])

        if not messages:
            raise RuntimeError("RAG workflow returned no messages.")

        response_type: Literal["answer", "clarification"] = (
            "clarification" if final_state.next else "answer"
        )

        return ChatResponse(
            type=response_type,
            message=str(messages[-1].content),
            session_id=session_id,
        )
