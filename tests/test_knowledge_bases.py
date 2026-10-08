from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from project.api import auth_tokens
from project.db.business_models import KnowledgeBase


@pytest.fixture(autouse=True)
def signing_key(monkeypatch):
    monkeypatch.setattr(auth_tokens, "load_dotenv", lambda *_: None)
    monkeypatch.setenv("KNOWFLOW_JWT_SECRET", "ab" * 32)


@pytest.fixture
def accounts(client):
    result = []
    for _ in range(2):
        credentials = {"email": f"kb-{uuid4().hex}@example.com",
                       "password": "test-only knowledge base password"}
        registered = client.post("/auth/register", json=credentials)
        assert registered.status_code == 201
        login = client.post("/auth/login", json=credentials)
        assert login.status_code == 200
        result.append((registered.json()["id"], {
            "Authorization": f"Bearer {login.json()['access_token']}",
        }))
    return result


def test_create_persists_authenticated_owner_and_public_fields(client, accounts, db_session):
    owner, headers = accounts[0]
    response = client.post("/knowledge-bases", json={"name": "  我的知识库  "}, headers=headers)
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"id", "owner_id", "name", "created_at"}
    assert body["owner_id"] == owner
    assert body["name"] == "我的知识库"
    saved = db_session.get(KnowledgeBase, UUID(body["id"]))
    assert str(saved.owner_id) == owner
    assert saved.name == body["name"]
    detail = client.get(f"/knowledge-bases/{body['id']}", headers=headers)
    assert detail.status_code == 200
    assert detail.json() == body
    assert detail.headers["Cache-Control"] == "no-store"


def test_users_cannot_read_or_list_each_others_knowledge_bases(client, accounts):
    records = []
    for _, headers in accounts:
        response = client.post("/knowledge-bases", json={"name": "private"}, headers=headers)
        assert response.status_code == 201
        records.append(response.json())
    for index, (_, headers) in enumerate(accounts):
        own, other = records[index], records[1 - index]
        listing = client.get("/knowledge-bases", headers=headers)
        assert listing.status_code == 200
        assert listing.json() == [own]
        assert client.get(f"/knowledge-bases/{own['id']}", headers=headers).status_code == 200
        forbidden = client.get(f"/knowledge-bases/{other['id']}", headers=headers)
        missing = client.get(f"/knowledge-bases/{uuid4()}", headers=headers)
        assert forbidden.status_code == missing.status_code == 404
        assert forbidden.json() == missing.json() == {"detail": "Knowledge base not found"}


def test_client_cannot_choose_owner_or_id(client, accounts, db_session):
    owner, headers = accounts[0]
    other, _ = accounts[1]
    for extra in ({"owner_id": other}, {"id": str(uuid4())}):
        response = client.post("/knowledge-bases", json={"name": "forged", **extra}, headers=headers)
        assert response.status_code == 422
    assert db_session.scalar(select(KnowledgeBase.id).where(
        KnowledgeBase.owner_id == UUID(owner))) is None
    # Query parameters and headers cannot override authenticated ownership.
    response = client.post("/knowledge-bases", params={"owner_id": other},
                           headers={**headers, "X-User-ID": other}, json={"name": "mine"})
    assert response.status_code == 201
    assert response.json()["owner_id"] == owner
    listing = client.get("/knowledge-bases", params={"owner_id": other}, headers=headers)
    assert listing.json() == [response.json()]


@pytest.mark.parametrize("authorization", [None, "Bearer invalid-token"])
@pytest.mark.parametrize("method,path", [
    ("POST", "/knowledge-bases"), ("GET", "/knowledge-bases"),
    ("GET", f"/knowledge-bases/{uuid4()}"),
])
def test_all_endpoints_require_valid_auth(client, method, path, authorization):
    headers = {} if authorization is None else {"Authorization": authorization}
    response = client.request(method, path, headers=headers,
                              **({"json": {"name": "test"}} if method == "POST" else {}))
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize("payload", [{}, {"name": ""}, {"name": " \t "},
                                     {"name": "x" * 201}, {"name": None}, {"name": 123}])
def test_invalid_names_rejected_without_writes(client, accounts, payload):
    _, headers = accounts[0]
    assert client.post("/knowledge-bases", json=payload, headers=headers).status_code == 422
    assert client.get("/knowledge-bases", headers=headers).json() == []


def test_list_pagination_is_scoped_and_deterministic(client, accounts):
    _, headers = accounts[0]
    for name in ("one", "two", "three"):
        assert client.post("/knowledge-bases", json={"name": name}, headers=headers).status_code == 201
    all_items = client.get("/knowledge-bases", headers=headers).json()
    assert len(all_items) == 3
    page = client.get("/knowledge-bases?limit=1&offset=1", headers=headers)
    assert page.json() == all_items[1:2]
    assert client.get("/knowledge-bases?offset=3", headers=headers).json() == []
    for query in ("limit=0", "limit=101", "offset=-1"):
        assert client.get(f"/knowledge-bases?{query}", headers=headers).status_code == 422
    assert client.get("/knowledge-bases/not-a-uuid", headers=headers).status_code == 422
