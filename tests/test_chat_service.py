from types import SimpleNamespace
from uuid import UUID

from langchain_core.messages import AIMessage

from project.api.chat_service import LangGraphChatService
from project.api.schemas import ChatRequest


class FakeGraph:
    def __init__(self, initial_state, final_state) -> None:
        self._states = iter([initial_state, final_state])
        self.invocations = []
        self.updates = []

    def get_state(self, config):
        return next(self._states)

    def invoke(self, graph_input, *, config) -> None:
        self.invocations.append((graph_input, config))

    def update_state(self, config, values) -> None:
        self.updates.append((config, values))


class FakeRAGSystem:
    def __init__(self, graph: FakeGraph) -> None:
        self.agent_graph = graph
        self.requested_thread_ids = []

    def get_config(self, thread_id=None):
        self.requested_thread_ids.append(thread_id)
        return {"configurable": {"thread_id": thread_id}}


def make_state(*, next_nodes=(), message=None):
    messages = [] if message is None else [AIMessage(content=message)]
    return SimpleNamespace(
        next=next_nodes,
        values={"messages": messages},
    )


def test_new_question_returns_answer_without_real_rag() -> None:
    graph = FakeGraph(
        initial_state=make_state(),
        final_state=make_state(message="测试答案"),
    )
    rag_system = FakeRAGSystem(graph)
    factory_calls = []

    def create_fake_rag_system() -> FakeRAGSystem:
        factory_calls.append(True)
        return rag_system

    service = LangGraphChatService(
        rag_system_factory=create_fake_rag_system,
    )

    assert factory_calls == []

    response = service.chat(ChatRequest(message="什么是混合检索？"))

    assert factory_calls == [True]
    assert response.type == "answer"
    assert response.message == "测试答案"
    assert isinstance(response.session_id, UUID)
    assert rag_system.requested_thread_ids == [str(response.session_id)]

    graph_input, config = graph.invocations[0]
    assert graph_input["messages"][0].content == "什么是混合检索？"
    assert config["configurable"]["thread_id"] == str(response.session_id)
    assert graph.updates == []


def test_unclear_question_returns_clarification() -> None:
    graph = FakeGraph(
        initial_state=make_state(),
        final_state=make_state(
            next_nodes=("request_clarification",),
            message="你说的“它”指什么？",
        ),
    )
    rag_system = FakeRAGSystem(graph)
    service = LangGraphChatService(
        rag_system_factory=lambda: rag_system,
    )

    response = service.chat(ChatRequest(message="它有什么优点？"))

    assert response.type == "clarification"
    assert response.message == "你说的“它”指什么？"
    assert graph.updates == []

    graph_input, _ = graph.invocations[0]
    assert graph_input["messages"][0].content == "它有什么优点？"


def test_clarification_reply_resumes_existing_session() -> None:
    session_id = UUID("550e8400-e29b-41d4-a716-446655440000")
    graph = FakeGraph(
        initial_state=make_state(
            next_nodes=("request_clarification",),
            message="你说的“它”指什么？",
        ),
        final_state=make_state(
            message="混合检索结合了关键词检索和向量检索。",
        ),
    )
    rag_system = FakeRAGSystem(graph)
    service = LangGraphChatService(
        rag_system_factory=lambda: rag_system,
    )

    response = service.chat(
        ChatRequest(
            message="混合检索",
            session_id=session_id,
        )
    )

    assert response.type == "answer"
    assert response.session_id == session_id
    assert response.message == "混合检索结合了关键词检索和向量检索。"
    assert rag_system.requested_thread_ids == [str(session_id)]

    update_config, update_values = graph.updates[0]
    assert update_config["configurable"]["thread_id"] == str(session_id)
    assert update_values["messages"][0].content == "混合检索"

    graph_input, invoke_config = graph.invocations[0]
    assert graph_input is None
    assert invoke_config == update_config
