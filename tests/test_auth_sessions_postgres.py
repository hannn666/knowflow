"""Opt-in real PostgreSQL migration/row-lock tests in a UUID-owned database.

KNOWFLOW_TEST_AUTH_POSTGRES=1 enables this module. Never migrate the dev DB.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import os
from pathlib import Path
from threading import Barrier, Event
from unittest.mock import patch
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.orm import Session

from project.api import auth_session_service as service, auth_tokens
from project.db.business_database import get_database_url
from project.db.business_models import AuthRefreshToken, AuthSession, Document, DocumentParseStage, DocumentVersion, KnowledgeBase, User
from test_login import signing_key


@pytest.fixture(scope="module")
def pg_engine():
    if os.getenv("KNOWFLOW_TEST_AUTH_POSTGRES") != "1":
        pytest.skip("Explicit isolated auth PostgreSQL tests required")
    source = get_database_url()
    assert (source.host, source.port, source.database) == ("127.0.0.1", 5432, "knowflow")
    name = "knowflow_test_auth_" + uuid4().hex
    target = source.set(database=name)
    admin = create_engine(source.set(database="postgres"), isolation_level="AUTOCOMMIT", hide_parameters=True)
    engine = None
    created = False
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "migrations"))
    try:
        with admin.connect() as c:
            assert c.scalar(text("SELECT current_database()")) == "postgres"
            c.execute(text(f'CREATE DATABASE "{name}"'))
            created = True
        print("Created owned auth test database: " + name)
        engine = create_engine(target, hide_parameters=True)
        with engine.connect() as c:
            assert c.scalar(text("SELECT current_database()")) == name
            assert c.scalar(text("SHOW transaction_isolation")) == "read committed"
        with patch("project.db.business_database.get_database_url", return_value=target):
            command.upgrade(config, "c842a9f713e0")
            marker = uuid4()
            with engine.begin() as c:
                c.execute(text("INSERT INTO users (id,email,password_hash) VALUES (:id,:email,'test-only')"),
                          {"id": marker, "email": f"marker-{marker}@example.com"})
                kb, doc, ver = uuid4(), uuid4(), uuid4()
                c.execute(KnowledgeBase.__table__.insert().values(id=kb, owner_id=marker, name="migration marker"))
                c.execute(Document.__table__.insert().values(id=doc, knowledge_base_id=kb))
                c.execute(DocumentVersion.__table__.insert().values(id=ver, document_id=doc, original_filename="test-only.pdf"))
                c.execute(DocumentParseStage.__table__.insert().values(document_version_id=ver,
                    attempt_id=uuid4(), status="failed", started_at=service.utcnow(),
                    finished_at=service.utcnow(), error_code="processing_failed"))
            def snapshot():
                with engine.connect() as c:
                    return {t: c.execute(text(f'SELECT * FROM "{t}"')).all()
                            for t in ("users", "knowledge_bases", "documents", "document_versions", "document_parse_stages")}
            before = snapshot()
            command.upgrade(config, "head")
            assert snapshot() == before
            assert {"auth_sessions", "auth_refresh_tokens"} <= set(inspect(engine).get_table_names())
            assert next(i for i in inspect(engine).get_indexes("auth_refresh_tokens")
                        if i["name"] == "uq_auth_refresh_active")["unique"]
            command.check(config)
            command.downgrade(config, "c842a9f713e0")
            assert snapshot() == before
            command.upgrade(config, "head")
            command.check(config)
        yield engine
    finally:
        if engine is not None:
            engine.dispose()
        try:
            if created:
                # Only this exact generated DB; never FORCE/terminate other sessions.
                assert name.startswith("knowflow_test_auth_") and len(name.rsplit("_", 1)[1]) == 32
                with admin.connect() as c:
                    c.execute(text(f'DROP DATABASE "{name}"'))
                print("Removed owned auth test database: " + name)
        finally:
            admin.dispose()


def issue(engine):
    with Session(engine) as s:
        user = User(id=uuid4(), email=f"auth-pg-{uuid4().hex}@example.com", password_hash="test-only")
        s.add(user)
        s.commit()
        return service.start_session(s, user.id)


def actor(engine, operation, raw):
    with Session(engine) as s:
        try:
            return operation(s, raw)
        except service.InvalidSession:
            return "denied"


@pytest.mark.parametrize("change", ["revoke", "rotate", "replay"])
def test_long_result_gate_observes_committed_session_change(pg_engine, change):
    """Independent DB connections, bounded events; no model or business data."""
    bundle = issue(pg_engine)
    identity = auth_tokens.decode_access_identity(bundle.access_token)
    admitted, completed = Event(), Event()

    def long_request():
        with Session(pg_engine) as s:
            service.check_session(s, identity, activity=True)
            stale = s.get(AuthSession, identity.session_id)
            original_idle = stale.idle_expires_at
            s.rollback()  # Same boundary used before grounded model execution.
            assert not s.in_transaction()
            admitted.set()
            assert completed.wait(15)
            try:
                service.check_result_session(s, identity)
                verdict = "allowed"
            except service.InvalidSession:
                verdict = "denied"
            assert not s.in_transaction()
            assert s.get(AuthSession, identity.session_id, populate_existing=True).idle_expires_at == original_idle
            return verdict

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = pool.submit(long_request)
        assert admitted.wait(15)
        try:
            operation = service.revoke_session if change == "revoke" else service.rotate_refresh
            # Must finish while inference remains paused: no auth row lock held.
            pool.submit(actor, pg_engine, operation, bundle.refresh_token).result(timeout=5)
            if change == "replay":
                assert pool.submit(actor, pg_engine, service.rotate_refresh,
                                   bundle.refresh_token).result(timeout=5) == "denied"
        finally:
            completed.set()
        assert result.result(timeout=15) == ("allowed" if change == "rotate" else "denied")


@pytest.mark.parametrize("round_number", range(3))
def test_same_token_concurrent_rotation_is_single_use(pg_engine, monkeypatch, round_number):
    bundle = issue(pg_engine)
    barrier = Barrier(2, timeout=15)
    original = service._locked
    def gated(s, sid):
        barrier.wait()  # Both actors looked up the same RT before either locks.
        return original(s, sid)
    monkeypatch.setattr(service, "_locked", gated)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(actor, pg_engine, service.rotate_refresh, bundle.refresh_token) for _ in range(2)]
        results = [f.result(timeout=25) for f in futures]
    monkeypatch.setattr(service, "_locked", original)
    assert results.count("denied") == 1
    successor = next(r for r in results if r != "denied")
    with Session(pg_engine) as s:
        row = s.get(AuthSession, bundle.session_id)
        assert row.revocation_reason == "replay"
        tokens = s.scalars(select(AuthRefreshToken).where(AuthRefreshToken.session_id == bundle.session_id)).all()
        assert len(tokens) == 2 and sum(t.consumed_at is None for t in tokens) == 1
        with pytest.raises(service.InvalidSession):
            service.check_session(s, auth_tokens.decode_access_identity(successor.access_token))
        with pytest.raises(service.InvalidSession):
            service.rotate_refresh(s, successor.refresh_token)


@pytest.mark.parametrize("round_number", range(3))
def test_logout_racing_rotation_cannot_resurrect_session(pg_engine, monkeypatch, round_number):
    bundle = issue(pg_engine)
    barrier = Barrier(2, timeout=15)
    original = service._locked
    def gated(s, sid):
        barrier.wait()
        return original(s, sid)
    monkeypatch.setattr(service, "_locked", gated)
    with ThreadPoolExecutor(max_workers=2) as pool:
        rotation = pool.submit(actor, pg_engine, service.rotate_refresh, bundle.refresh_token)
        logout = pool.submit(actor, pg_engine, service.revoke_session, bundle.refresh_token)
        result, _ = rotation.result(timeout=25), logout.result(timeout=25)
    monkeypatch.setattr(service, "_locked", original)
    with Session(pg_engine) as s:
        assert s.get(AuthSession, bundle.session_id).revocation_reason == "logout"
        raw = bundle.refresh_token if result == "denied" else result.refresh_token
        with pytest.raises(service.InvalidSession):
            service.rotate_refresh(s, raw)


def test_expiry_is_checked_after_row_lock_wait(pg_engine, monkeypatch):
    bundle = issue(pg_engine)
    with Session(pg_engine) as blocker:
        row = blocker.scalar(select(AuthSession).where(AuthSession.id == bundle.session_id).with_for_update())
        expiry = row.idle_expires_at
        original = service._locked
        arrived = Event()
        def waiting(s, sid):
            arrived.set()
            return original(s, sid)
        monkeypatch.setattr(service, "_locked", waiting)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(actor, pg_engine, service.rotate_refresh, bundle.refresh_token)
                assert arrived.wait(timeout=10)
                monkeypatch.setattr(service, "utcnow", lambda: expiry)
                blocker.rollback()
                assert future.result(timeout=20) == "denied"
        finally:
            blocker.rollback()
    with Session(pg_engine) as s:
        assert s.scalar(select(AuthRefreshToken.consumed_at).where(
            AuthRefreshToken.token_hash == service.token_digest(bundle.refresh_token))) is None


def test_rotation_failure_rolls_back_in_another_connection(pg_engine, monkeypatch):
    bundle = issue(pg_engine)
    original = service._issue
    def broken(*args):
        raise RuntimeError("test-only signing failure")
    monkeypatch.setattr(service, "_issue", broken)
    with Session(pg_engine) as s, pytest.raises(RuntimeError):
        service.rotate_refresh(s, bundle.refresh_token)
    with Session(pg_engine) as s:
        assert s.scalar(select(func.count()).select_from(AuthRefreshToken).where(
            AuthRefreshToken.session_id == bundle.session_id)) == 1
        assert s.scalar(select(AuthRefreshToken.consumed_at).where(
            AuthRefreshToken.token_hash == service.token_digest(bundle.refresh_token))) is None
    monkeypatch.setattr(service, "_issue", original)
    assert actor(pg_engine, service.rotate_refresh, bundle.refresh_token) != "denied"


def test_database_rejects_fork_and_cascades_deleted_user(pg_engine):
    from sqlalchemy.exc import IntegrityError
    bundle = issue(pg_engine)
    with Session(pg_engine) as s:
        row = s.get(AuthSession, bundle.session_id)
        s.add(AuthRefreshToken(id=uuid4(), session_id=row.id, token_hash="a" * 64,
              created_at=row.created_at, expires_at=row.absolute_expires_at))
        with pytest.raises(IntegrityError):
            s.commit()
        s.rollback()
        service.rotate_refresh(s, bundle.refresh_token)
        s.delete(s.get(User, s.get(AuthSession, bundle.session_id).user_id))
        s.commit()
        assert s.get(AuthSession, bundle.session_id) is None
        assert s.scalar(select(func.count()).select_from(AuthRefreshToken).where(
            AuthRefreshToken.session_id == bundle.session_id)) == 0


def test_real_database_http_login_refresh_logout_and_isolation(pg_engine):
    from fastapi.testclient import TestClient
    from project.api.app import create_app
    from project.db.business_database import get_database_session
    app = create_app()
    def sessions():
        with Session(pg_engine) as s:
            yield s
    app.dependency_overrides[get_database_session] = sessions
    with TestClient(app) as client:
        accounts = []
        for _ in range(2):
            credentials = {"email": f"http-auth-{uuid4().hex}@example.com", "password": "test-only session password"}
            assert client.post("/auth/register", json=credentials).status_code == 201
            response = client.post("/auth/login", json=credentials)
            assert response.status_code == 200
            accounts.append(response.json())
        first, other = accounts
        headers = {"Authorization": "Bearer " + first["access_token"]}
        # Every HTTP request owns an independent real PostgreSQL transaction.
        assert client.get("/auth/me", headers=headers).status_code == 200
        newer = client.post("/auth/refresh", json={"refresh_token": first["refresh_token"]}).json()
        assert newer["session_id"] == first["session_id"]
        assert client.post("/auth/logout", json={"refresh_token": newer["refresh_token"]}).status_code == 204
        assert client.get("/auth/me", headers=headers).status_code == 401
        assert client.post("/auth/refresh", json={"refresh_token": newer["refresh_token"]}).status_code == 401
        assert client.get("/auth/me", headers={"Authorization": "Bearer " + other["access_token"]}).status_code == 200


def test_activity_racing_logout_cannot_unrevoke(pg_engine, monkeypatch):
    bundle = issue(pg_engine)
    identity = auth_tokens.decode_access_identity(bundle.access_token)
    barrier = Barrier(2, timeout=15)
    original = service._locked
    def gated(s, sid):
        barrier.wait()
        return original(s, sid)
    monkeypatch.setattr(service, "_locked", gated)
    with ThreadPoolExecutor(max_workers=2) as pool:
        activity = pool.submit(actor, pg_engine, lambda s, _: service.check_session(s, identity, activity=True), "")
        logout = pool.submit(actor, pg_engine, service.revoke_session, bundle.refresh_token)
        activity.result(timeout=25)
        assert logout.result(timeout=25) is None
    monkeypatch.setattr(service, "_locked", original)
    with Session(pg_engine) as s:
        assert s.get(AuthSession, bundle.session_id).revocation_reason == "logout"
        with pytest.raises(service.InvalidSession):
            service.check_session(s, identity, activity=True)
