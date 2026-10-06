from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from project.api.passwords import verify_password
from project.db.business_models import User


def test_registration_persists_hash_and_returns_only_public_fields(
    client: TestClient, db_session: Session, credentials: dict[str, str]
) -> None:
    response = client.post("/auth/register", json=credentials)

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"id", "email", "created_at"}
    assert body["email"] == credentials["email"]
    assert body["created_at"]
    user = db_session.get(User, UUID(body["id"]))
    assert user is not None
    assert user.email == credentials["email"]
    assert user.password_hash.startswith("$argon2id$")
    assert verify_password(credentials["password"], user.password_hash)
    assert credentials["password"] not in response.text
    assert user.password_hash not in response.text


def test_duplicate_email_is_case_insensitive_and_transaction_recovers(
    client: TestClient, db_session: Session, credentials: dict[str, str]
) -> None:
    first = client.post("/auth/register", json=credentials)
    assert first.status_code == 201
    duplicate = client.post("/auth/register", json={
        "email": f"  {credentials['email'].upper()}  ",
        "password": "a different registration phrase",
    })

    assert duplicate.status_code == 409
    assert duplicate.json() == {"detail": "Email already registered"}
    users = db_session.scalars(
        select(User).where(User.email == credentials["email"])
    ).all()
    assert len(users) == 1
    assert verify_password(credentials["password"], users[0].password_hash)
    another = dict(credentials, email=f"another-{uuid4().hex}@example.com")
    assert client.post("/auth/register", json=another).status_code == 201


@pytest.mark.parametrize("changes", [
    {"email": "not-an-email"},
    {"password": "short-secret"},
    {"password": "x" * 129},
    {"password": None},
    {"id": "client-cannot-choose-an-id"},
    {"password_hash": "client-cannot-supply-a-hash"},
])
def test_invalid_registration_does_not_echo_inputs_or_create_user(
    client: TestClient, db_session: Session,
    credentials: dict[str, str], changes: dict,
) -> None:
    payload = credentials | changes
    response = client.post("/auth/register", json=payload)

    assert response.status_code == 422
    for error in response.json()["detail"]:
        assert set(error) == {"type", "loc", "msg"}
    for value in (payload.get("password"), payload.get("password_hash")):
        if value:
            assert value not in response.text
    assert db_session.scalar(
        select(User.id).where(User.email == credentials["email"])
    ) is None


def test_missing_fields_and_malformed_json_are_rejected(client: TestClient) -> None:
    assert client.post("/auth/register", json={}).status_code == 422
    marker = "malformed-json-secret"
    response = client.post(
        "/auth/register", content='{"password": "' + marker + '",',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert marker not in response.text


def test_password_spaces_are_preserved(
    client: TestClient, db_session: Session, credentials: dict[str, str]
) -> None:
    credentials["password"] = "  a long password with spaces  "
    response = client.post("/auth/register", json=credentials)
    assert response.status_code == 201
    user = db_session.get(User, UUID(response.json()["id"]))
    assert verify_password(credentials["password"], user.password_hash)
    assert not verify_password(credentials["password"].strip(), user.password_hash)


def test_unrelated_integrity_error_is_not_reported_as_duplicate_email(
    client: TestClient, db_session: Session,
    credentials: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_commit() -> None:
        raise IntegrityError("test statement", {}, RuntimeError("other constraint"))

    monkeypatch.setattr(db_session, "commit", fail_commit)
    with pytest.raises(IntegrityError):
        client.post("/auth/register", json=credentials)
