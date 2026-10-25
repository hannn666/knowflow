"""Session HTTP/control-flow tests; PostgreSQL locking is tested separately."""
from datetime import timedelta
import json
from uuid import UUID, uuid4

import jwt
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from project.api import auth_session_service as service, auth_tokens
from project.db.business_models import AuthSession, AuthRefreshToken
from test_login import signing_key, TEST_KEY


def login(client, credentials):
    assert client.post("/auth/register", json=credentials).status_code == 201
    response = client.post("/auth/login", json=credentials)
    assert response.status_code == 200
    return response.json()


def bearer(bundle):
    return {"Authorization": "Bearer " + bundle["access_token"]}


def refresh(client, bundle):
    return client.post("/auth/refresh", json={"refresh_token": bundle["refresh_token"]})


def family(db, bundle):
    return db.get(AuthSession, UUID(bundle["session_id"]), populate_existing=True)


def test_issue_digest_only_and_stable_family(client, db_session, credentials, caplog):
    first = login(client, credentials)
    row = family(db_session, first)
    assert (service.aware(row.absolute_expires_at) - service.aware(row.created_at)).total_seconds() == 28800
    assert (service.aware(row.idle_expires_at) - service.aware(row.created_at)).total_seconds() == 1800
    issued = db_session.scalar(select(AuthRefreshToken))
    assert len(first["refresh_token"]) == 43
    assert issued.token_hash == service.token_digest(first["refresh_token"])
    assert first["refresh_token"] not in repr(issued.__dict__) + caplog.text
    assert first["access_token"] not in repr(issued.__dict__) + caplog.text
    second_response = refresh(client, first)
    assert second_response.status_code == 200
    second = second_response.json()
    assert first["refresh_token"] != second["refresh_token"]
    assert first["session_id"] == second["session_id"]
    assert first["session_expires_at"] == second["session_expires_at"]
    assert first["idle_expires_at"] == second["idle_expires_at"]
    tokens = db_session.scalars(select(AuthRefreshToken)).all()
    assert len(tokens) == 2 and sum(t.consumed_at is None for t in tokens) == 1
    assert next(t for t in tokens if t.consumed_at is None).parent_token_id == issued.id
    assert client.get("/auth/me", headers=bearer(first)).status_code == 200
    assert client.get("/auth/me", headers=bearer(second)).status_code == 200
    assert auth_tokens.decode_access_identity(second["access_token"]).session_id == row.id
    assert second_response.headers["cache-control"] == "no-store"


def test_replay_persists_revocation_and_only_affects_own_family(client, db_session, credentials):
    first = login(client, credentials)
    other_device = client.post("/auth/login", json=credentials).json()
    other_user = login(client, {**credentials, "email": f"other-{uuid4().hex}@example.com"})
    second = refresh(client, first).json()
    assert refresh(client, first).status_code == 401
    # A later transaction observes the committed revoke, not an in-memory flag.
    db_session.rollback()
    assert family(db_session, first).revocation_reason == "replay"
    assert refresh(client, second).status_code == 401
    assert client.get("/auth/me", headers=bearer(first)).status_code == 401
    assert client.get("/auth/me", headers=bearer(second)).status_code == 401
    for valid in (other_device, other_user):
        assert client.get("/auth/me", headers=bearer(valid)).status_code == 200
        assert refresh(client, valid).status_code == 200


def test_logout_by_consumed_token_is_idempotent(client, db_session, credentials):
    first = login(client, credentials)
    second = refresh(client, first).json()
    for _ in range(2):
        response = client.post("/auth/logout", json={"refresh_token": first["refresh_token"]})
        assert response.status_code == 204 and response.content == b""
        assert response.headers["cache-control"] == "no-store"
    assert family(db_session, first).revocation_reason == "logout"
    assert refresh(client, second).status_code == 401
    assert client.get("/auth/me", headers=bearer(second)).status_code == 401


@pytest.mark.parametrize("path", ["/auth/refresh", "/auth/logout"])
@pytest.mark.parametrize("payload", [{}, {"refresh_token": "secret"},
    {"refresh_token": 42}, {"refresh_token": "x" * 5000},
    {"refresh_token": "x" * 43, "user_id": "private-marker"}])
def test_refresh_validation_is_private(client, path, payload):
    response = client.post(path, json=payload)
    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert all(set(e) == {"type", "loc", "msg"} for e in response.json()["detail"])
    assert "private-marker" not in response.text and '"input"' not in response.text


@pytest.mark.parametrize("raw", ["x" * 43, "+" * 43])
def test_unknown_token_cannot_revoke_target(client, db_session, credentials, raw):
    first = login(client, credentials)
    for path in ("/auth/refresh", "/auth/logout"):
        response = client.post(path, json={"refresh_token": raw})
        assert response.status_code == 401 and raw not in response.text
    assert family(db_session, first).revoked_at is None
    assert client.get("/auth/me", headers=bearer(first)).status_code == 200
    # RT is not a business Access Token.
    assert client.get("/auth/me", headers={"Authorization": "Bearer " + first["refresh_token"]}).status_code == 401


@pytest.mark.parametrize("delta", [1800, 1801, 28800])
def test_idle_expiry_cannot_be_extended_by_refresh_or_me(client, db_session, credentials, monkeypatch, delta):
    first = login(client, credentials)
    start = service.aware(family(db_session, first).created_at)
    monkeypatch.setattr(service, "utcnow", lambda: start + timedelta(seconds=delta))
    assert refresh(client, first).status_code == 401
    assert client.get("/auth/me", headers=bearer(first)).status_code == 401
    assert service.aware(family(db_session, first).idle_expires_at) == start + timedelta(seconds=1800)


def test_business_activity_extends_idle_but_never_absolute(client, db_session, credentials, monkeypatch):
    first = login(client, credentials)
    identity = auth_tokens.decode_access_identity(first["access_token"])
    start = service.aware(family(db_session, first).created_at)
    for seconds in range(1700, 28800, 1700):
        monkeypatch.setattr(service, "utcnow", lambda seconds=seconds: start + timedelta(seconds=seconds))
        service.check_session(db_session, identity, activity=True)
        rotated = service.rotate_refresh(db_session, first["refresh_token"])
        first["refresh_token"] = rotated.refresh_token
        row = family(db_session, first)
        assert service.aware(row.idle_expires_at) <= start + timedelta(hours=8)
        assert service.aware(row.absolute_expires_at) == start + timedelta(hours=8)
    monkeypatch.setattr(service, "utcnow", lambda: start + timedelta(hours=8))
    with pytest.raises(service.InvalidSession):
        service.check_session(db_session, identity, activity=True)
    assert refresh(client, first).status_code == 401


def test_me_does_not_touch_idle_business_request_does(client, db_session, credentials, monkeypatch):
    first = login(client, credentials)
    row = family(db_session, first)
    initial = service.aware(row.idle_expires_at)
    monkeypatch.setattr(service, "utcnow", lambda: service.aware(row.created_at) + timedelta(minutes=10))
    assert client.get("/auth/me", headers=bearer(first)).status_code == 200
    assert service.aware(family(db_session, first).idle_expires_at) == initial
    assert client.get("/knowledge-bases", headers=bearer(first)).status_code == 200
    assert service.aware(family(db_session, first).idle_expires_at) == initial + timedelta(minutes=10)


@pytest.mark.parametrize("changes", [{"sid": None}, {"sid": "wrong"}, {"sid": str(uuid4())},
    {"sub": str(uuid4())}, {"token_use": "refresh"}])
def test_session_binding_is_mandatory(client, credentials, changes):
    first = login(client, credentials)
    claims = jwt.decode(first["access_token"], TEST_KEY, algorithms=["HS256"], audience="knowflow-api")
    claims.update(changes)
    raw = jwt.encode(claims, TEST_KEY, algorithm="HS256")
    assert client.get("/auth/me", headers={"Authorization": "Bearer " + raw}).status_code == 401


def test_legacy_jwt_requires_relogin_and_expired_access_cannot_authenticate(client, credentials):
    first = login(client, credentials)
    claims = jwt.decode(first["access_token"], TEST_KEY, algorithms=["HS256"], audience="knowflow-api")
    claims.pop("sid")
    raw = jwt.encode(claims, TEST_KEY, algorithm="HS256")
    with pytest.raises(jwt.InvalidTokenError):
        auth_tokens.decode_access_identity(raw)
    assert client.get("/auth/me", headers={"Authorization": "Bearer " + raw}).status_code == 401
    claims["sid"] = first["session_id"]
    claims.update(iat=1, exp=2)
    raw = jwt.encode(claims, TEST_KEY, algorithm="HS256")
    assert client.get("/auth/me", headers={"Authorization": "Bearer " + raw}).status_code == 401
    assert refresh(client, first).status_code == 200


@pytest.mark.parametrize("operation", ["issue", "rotate"])
def test_signing_failure_rolls_back_all_credentials(client, db_session, credentials, monkeypatch, operation):
    first = login(client, credentials)
    before = {table: db_session.scalar(select(func.count()).select_from(table))
              for table in (AuthSession, AuthRefreshToken)}
    def fail(*args):
        raise RuntimeError("private signing sentinel")
    monkeypatch.setattr(service, "create_access_token", fail)
    response = (client.post("/auth/login", json=credentials) if operation == "issue" else refresh(client, first))
    assert response.status_code == 503 and "sentinel" not in response.text
    for table, count in before.items():
        assert db_session.scalar(select(func.count()).select_from(table)) == count
    assert db_session.scalar(select(AuthRefreshToken)).consumed_at is None


@pytest.mark.parametrize("operation", ["issue", "rotate", "logout", "replay"])
def test_commit_failure_never_reports_success(client, db_session, credentials, monkeypatch, operation):
    first = login(client, credentials)
    if operation == "replay":
        assert refresh(client, first).status_code == 200
    before = db_session.scalar(select(func.count()).select_from(AuthRefreshToken))
    original = db_session.commit
    def fail():
        raise RuntimeError("private DB sentinel")
    monkeypatch.setattr(db_session, "commit", fail)
    if operation == "issue":
        response = client.post("/auth/login", json=credentials)
    elif operation == "logout":
        response = client.post("/auth/logout", json={"refresh_token": first["refresh_token"]})
    else:
        response = refresh(client, first)
    assert response.status_code == 503 and "sentinel" not in response.text
    monkeypatch.setattr(db_session, "commit", original)
    assert db_session.scalar(select(func.count()).select_from(AuthRefreshToken)) == before
    assert family(db_session, first).revoked_at is None
    if operation != "replay":
        assert db_session.scalar(select(AuthRefreshToken)).consumed_at is None


def test_unknown_sid_does_not_touch_foreign_family(client, db_session, credentials):
    first = login(client, credentials)
    other = login(client, {**credentials, "email": f"other-{uuid4().hex}@example.com"})
    claims = jwt.decode(first["access_token"], TEST_KEY, algorithms=["HS256"], audience="knowflow-api")
    claims["sid"] = other["session_id"]
    raw = jwt.encode(claims, TEST_KEY, algorithm="HS256")
    assert client.get("/knowledge-bases", headers={"Authorization": "Bearer " + raw}).status_code == 401
    assert family(db_session, other).revoked_at is None


def test_one_unconsumed_token_per_session_is_database_constraint(client, db_session, credentials):
    first = login(client, credentials)
    old = db_session.scalar(select(AuthRefreshToken))
    db_session.add(AuthRefreshToken(id=uuid4(), session_id=old.session_id, token_hash="f" * 64,
        created_at=old.created_at, expires_at=old.expires_at))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


@pytest.mark.parametrize("operation", ["issue", "rotate", "logout", "check"])
def test_auth_transactions_do_not_commit_pending_business_changes(client, db_session, credentials, operation):
    from project.db.business_models import KnowledgeBase
    first = login(client, credentials)
    identity = auth_tokens.decode_access_identity(first["access_token"])
    pending = KnowledgeBase(id=uuid4(), owner_id=identity.user_id, name="uncommitted test-only")
    db_session.add(pending)
    actions = {"issue": lambda: service.start_session(db_session, identity.user_id),
               "rotate": lambda: service.rotate_refresh(db_session, first["refresh_token"]),
               "logout": lambda: service.revoke_session(db_session, first["refresh_token"]),
               "check": lambda: service.check_session(db_session, identity)}
    with pytest.raises(RuntimeError, match="clean unit of work"):
        actions[operation]()
    assert pending in db_session.new
    db_session.rollback()


def test_refresh_token_expiry_checked_independently(client, db_session, credentials):
    first = login(client, credentials)
    row = db_session.scalar(select(AuthRefreshToken))
    row.created_at = service.utcnow() - timedelta(minutes=2)
    row.expires_at = service.utcnow() - timedelta(minutes=1)
    db_session.commit()
    assert refresh(client, first).status_code == 401


def test_me_database_failure_fails_closed(client, db_session, credentials, monkeypatch):
    from sqlalchemy.exc import OperationalError
    first = login(client, credentials)
    def fail(*args, **kwargs):
        raise OperationalError("test-only", {}, RuntimeError("private DB sentinel"))
    monkeypatch.setattr(db_session, "scalar", fail)
    response = client.get("/auth/me", headers=bearer(first))
    assert response.status_code == 503 and "sentinel" not in response.text
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("path", ["/auth/refresh", "/auth/logout"])
def test_malformed_refresh_json_never_echoes_token(client, path):
    response = client.post(path, content='{"refresh_token":"private-marker",',
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 422 and "private-marker" not in response.text


@pytest.mark.parametrize("operation", ["issue", "rotate", "logout", "replay"])
def test_commit_ack_loss_is_reported_as_uncertain_not_rolled_back_success(client, db_session, credentials, monkeypatch, operation):
    first = login(client, credentials)
    if operation == "replay":
        assert refresh(client, first).status_code == 200
    before = db_session.scalar(select(func.count()).select_from(AuthRefreshToken))
    actual_commit = db_session.commit
    def lost_ack():
        actual_commit()
        raise RuntimeError("test-only acknowledgement lost")
    monkeypatch.setattr(db_session, "commit", lost_ack)
    if operation == "issue":
        response = client.post("/auth/login", json=credentials)
    elif operation == "logout":
        response = client.post("/auth/logout", json={"refresh_token": first["refresh_token"]})
    else:
        response = refresh(client, first)
    assert response.status_code == 503 and "acknowledgement" not in response.text
    assert "access_token" not in response.text and "refresh_token" not in response.text
    monkeypatch.setattr(db_session, "commit", actual_commit)
    if operation in ("issue", "rotate"):
        assert db_session.scalar(select(func.count()).select_from(AuthRefreshToken)) == before + 1
    if operation == "rotate":
        # The old secret cannot recover the lost successor; replay revokes it.
        assert refresh(client, first).status_code == 401
        assert family(db_session, first).revocation_reason == "replay"
    elif operation in ("logout", "replay"):
        assert family(db_session, first).revoked_at is not None
        assert client.get("/auth/me", headers=bearer(first)).status_code == 401
