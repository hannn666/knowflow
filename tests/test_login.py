from datetime import datetime, timezone
from uuid import UUID, uuid4

import jwt
import pytest
from project.api import auth_tokens
from project.db.business_models import User


# Public test-only key, never used as an application fallback.
TEST_KEY = "ab" * 32


@pytest.fixture(autouse=True)
def signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # Do not load local secrets during token tests.
    monkeypatch.setattr(auth_tokens, "load_dotenv", lambda *_: None)
    monkeypatch.setenv("KNOWFLOW_JWT_SECRET", TEST_KEY)


def token_for(user_id: str, **changes) -> str:
    now = int(datetime.now(timezone.utc).timestamp())
    claims = {
        "sub": user_id, "iat": now, "exp": now + 900,
        "iss": "knowflow", "aud": "knowflow-api", "token_use": "access",
    }
    claims.update(changes)
    return jwt.encode(claims, TEST_KEY, algorithm="HS256")


def test_login_then_me_returns_only_public_fields(client, credentials) -> None:
    registered = client.post("/auth/register", json=credentials).json()
    login = client.post("/auth/login", json={
        **credentials, "email": f"  {credentials['email'].upper()}  ",
    })
    assert login.status_code == 200
    assert login.headers["Cache-Control"] == "no-store"
    assert login.headers["Pragma"] == "no-cache"
    body = login.json()
    assert set(body) == {"access_token", "token_type", "expires_in"}
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 900
    assert str(auth_tokens.decode_access_token(body["access_token"])) == registered["id"]
    claims = jwt.decode(body["access_token"], TEST_KEY, algorithms=["HS256"],
                        audience="knowflow-api", issuer="knowflow")
    assert set(claims) == {"sub", "iat", "exp", "iss", "aud", "token_use"}
    response = client.get("/auth/me", headers={
        "Authorization": f"Bearer {body['access_token']}",
    })
    assert response.status_code == 200
    assert response.json() == registered
    assert response.headers["Cache-Control"] == "no-store"
    assert credentials["password"] not in login.text + response.text


def test_bad_password_and_unknown_account_have_same_error(client, credentials) -> None:
    assert client.post("/auth/register", json=credentials).status_code == 201
    wrong = client.post("/auth/login", json={**credentials, "password": "wrong"})
    unknown = client.post("/auth/login", json={
        **credentials, "email": f"missing-{uuid4().hex}@example.com",
    })
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json() == {"detail": "Incorrect email or password"}
    assert wrong.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize("authorization", [None, "Basic abc", "Bearer", "Bearer broken.token"])
def test_missing_or_malformed_credentials_rejected(client, authorization) -> None:
    headers = {} if authorization is None else {"Authorization": authorization}
    response = client.get("/auth/me", headers=headers)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize("changes", [
    {"iat": 1, "exp": 2}, {"iss": "another-service"}, {"aud": "another-api"},
    {"token_use": "refresh"}, {"sub": "not-a-uuid"}, {"sub": 123},
    {"iat": 9999999999, "exp": 10000000899}, {"exp": None},
])
def test_invalid_claims_rejected(client, credentials, changes) -> None:
    registered = client.post("/auth/register", json=credentials).json()
    claims = {"sub": registered["id"], **changes}
    token = token_for(claims.pop("sub"), **claims)
    response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert token not in response.text


@pytest.mark.parametrize("claim", ["sub", "iat", "exp", "iss", "aud", "token_use"])
def test_required_claims_cannot_be_omitted(client, credentials, claim) -> None:
    registered = client.post("/auth/register", json=credentials).json()
    token = token_for(registered["id"])
    claims = jwt.decode(token, TEST_KEY, algorithms=["HS256"], audience="knowflow-api")
    claims.pop(claim)
    token = jwt.encode(claims, TEST_KEY, algorithm="HS256")
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


@pytest.mark.parametrize("algorithm,key", [("HS256", "cd" * 32), ("HS384", TEST_KEY), ("none", "")])
def test_wrong_signature_or_algorithm_rejected(client, credentials, algorithm, key) -> None:
    registered = client.post("/auth/register", json=credentials).json()
    claims = jwt.decode(token_for(registered["id"]), TEST_KEY,
                        algorithms=["HS256"], audience="knowflow-api")
    token = jwt.encode(claims, key, algorithm=algorithm)
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_deleted_user_token_rejected(client, db_session, credentials) -> None:
    registered = client.post("/auth/register", json=credentials).json()
    token = client.post("/auth/login", json=credentials).json()["access_token"]
    db_session.delete(db_session.get(User, UUID(registered["id"])))
    db_session.commit()
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


@pytest.mark.parametrize("payload", [
    {}, {"email": "bad", "password": "private-test-input"},
    {"email": "valid@example.com", "password": "x" * 129},
    {"email": "valid@example.com", "password": None},
    {"email": "valid@example.com", "password": "secret", "owner_id": "fake"},
])
def test_login_validation_does_not_echo_inputs(client, payload) -> None:
    response = client.post("/auth/login", json=payload)
    assert response.status_code == 422
    assert all(set(error) == {"type", "loc", "msg"} for error in response.json()["detail"])
    if payload.get("password"):
        assert payload["password"] not in response.text


def test_login_malformed_json_does_not_echo_password(client) -> None:
    response = client.post("/auth/login", content='{"password":"private-marker",',
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert "private-marker" not in response.text


@pytest.mark.parametrize("key", [None, "", "short", "replace_with_64_random_hex_characters"])
def test_invalid_signing_configuration_fails_closed(monkeypatch, key) -> None:
    if key is None:
        monkeypatch.delenv("KNOWFLOW_JWT_SECRET")
    else:
        monkeypatch.setenv("KNOWFLOW_JWT_SECRET", key)
    with pytest.raises(RuntimeError, match="64 random hex"):
        auth_tokens.create_access_token(uuid4())


def test_health_and_registration_do_not_need_signing_key(client, credentials, monkeypatch) -> None:
    monkeypatch.delenv("KNOWFLOW_JWT_SECRET")
    assert client.get("/health").status_code == 200
    assert client.post("/auth/register", json=credentials).status_code == 201


def test_login_preserves_password_spaces(client, credentials) -> None:
    credentials["password"] = "  a long password with spaces  "
    assert client.post("/auth/register", json=credentials).status_code == 201
    assert client.post("/auth/login", json=credentials).status_code == 200
    assert client.post("/auth/login", json={
        **credentials, "password": credentials["password"].strip(),
    }).status_code == 401


def test_me_ignores_client_supplied_identity(client, credentials) -> None:
    first = client.post("/auth/register", json=credentials).json()
    second = client.post("/auth/register", json={
        **credentials, "email": f"other-{uuid4().hex}@example.com",
    }).json()
    token = client.post("/auth/login", json=credentials).json()["access_token"]
    response = client.get("/auth/me", params={"user_id": second["id"]}, headers={
        "Authorization": f"Bearer {token}", "X-User-ID": second["id"],
    })
    assert response.status_code == 200
    assert response.json()["id"] == first["id"]
