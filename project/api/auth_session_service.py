"""Session transaction boundary; never call with pending business writes.

PostgreSQL READ COMMITTED is required. All mutators lock the same family row,
then read the token again. No lock is held over a business operation or HTTP.
SQLite supports control-flow tests only, not concurrency guarantees.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import re
import secrets
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from project.api.auth_schemas import TokenResponse
from project.api.auth_tokens import ACCESS_TOKEN_SECONDS, AccessIdentity, create_access_token
from project.db.business_models import AuthRefreshToken, AuthSession, User

ABSOLUTE_SECONDS = 8 * 60 * 60
IDLE_SECONDS = 30 * 60


class InvalidSession(Exception):
    pass


def utcnow():
    return datetime.now(timezone.utc)


def aware(value):
    # SQLite drops timezone information; PostgreSQL returns aware UTC values.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def token_digest(raw):
    if not isinstance(raw, str) or re.fullmatch(r"[A-Za-z0-9_-]{43}", raw) is None:
        raise InvalidSession
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def _require_clean(session):
    # These functions own their short transaction. Never implicitly flush,
    # commit or roll back somebody else's pending business changes.
    if session.new or session.dirty or session.deleted:
        raise RuntimeError("Authentication requires a clean unit of work")


def _locked(session, identity):
    return session.scalar(select(AuthSession).where(AuthSession.id == identity)
                          .with_for_update().execution_options(populate_existing=True))


def _active(row, now):
    return (row is not None and row.revoked_at is None
            and now < aware(row.absolute_expires_at) and now < aware(row.idle_expires_at))


def _issue(session, family, now, parent=None):
    raw = secrets.token_urlsafe(32)
    session.add(AuthRefreshToken(id=uuid4(), session_id=family.id, token_hash=token_digest(raw),
        parent_token_id=parent, created_at=now, expires_at=family.absolute_expires_at))
    return TokenResponse(access_token=create_access_token(family.user_id, family.id),
        refresh_token=raw, expires_in=ACCESS_TOKEN_SECONDS, session_id=family.id,
        session_expires_at=aware(family.absolute_expires_at), idle_expires_at=aware(family.idle_expires_at))


def start_session(session: Session, user_id: UUID):
    _require_clean(session)
    try:
        now = utcnow()
        family = AuthSession(id=uuid4(), user_id=user_id, created_at=now,
            absolute_expires_at=now + timedelta(seconds=ABSOLUTE_SECONDS),
            idle_expires_at=now + timedelta(seconds=IDLE_SECONDS))
        session.add(family)
        session.flush()
        result = _issue(session, family, now)
        session.commit()
        return result
    except Exception:
        session.rollback()
        raise


def _find_family(session, raw):
    # Only a matching random secret identifies a revocation/refresh target.
    digest = token_digest(raw)
    sid = session.scalar(select(AuthRefreshToken.session_id).where(AuthRefreshToken.token_hash == digest))
    if sid is None:
        raise InvalidSession
    family = _locked(session, sid)
    token = session.scalar(select(AuthRefreshToken).where(AuthRefreshToken.token_hash == digest)
                           .execution_options(populate_existing=True))
    if family is None or token is None:
        raise InvalidSession
    return family, token


def rotate_refresh(session: Session, raw: str):
    _require_clean(session)
    try:
        family, old = _find_family(session, raw)
        now = utcnow()  # After acquiring the lock, never before waiting for it.
        if not _active(family, now) or session.get(User, family.user_id) is None:
            raise InvalidSession
        if old.consumed_at is not None:
            family.revoked_at, family.revocation_reason = now, "replay"
            session.commit()  # Must survive the subsequent 401/rollback.
            raise InvalidSession
        if now >= aware(old.expires_at):
            raise InvalidSession
        old.consumed_at = now
        session.flush()  # Release the partial unique active-token slot first.
        result = _issue(session, family, now, old.id)
        session.commit()
        return result
    except Exception:
        session.rollback()
        raise


def revoke_session(session: Session, raw: str):
    _require_clean(session)
    try:
        family, _ = _find_family(session, raw)
        # Even an expired/consumed RT may revoke only its own family. Idempotent.
        if family.revoked_at is None:
            family.revoked_at, family.revocation_reason = utcnow(), "logout"
        session.commit()
    except Exception:
        session.rollback()
        raise


def check_session(session: Session, identity: AccessIdentity, *, activity=False):
    """Check persisted state, optionally record a business-request admission.

    Commits only the short admission transaction, before endpoint business work.
    /me and refresh never move idle_expires_at. A DB failure must fail closed.
    """
    _require_clean(session)
    try:
        family = _locked(session, identity.session_id)
        now = utcnow()
        if not _active(family, now) or family.user_id != identity.user_id:
            raise InvalidSession
        if activity:
            family.idle_expires_at = min(now + timedelta(seconds=IDLE_SECONDS),
                                         aware(family.absolute_expires_at))
        session.commit()  # Release family lock before long-running business code.
    except Exception:
        session.rollback()
        raise


def check_result_session(session: Session, identity: AccessIdentity):
    """Release gate for an already admitted request, never a bearer endpoint.

    Start a fresh, short read transaction; select columns, not cached ORM state.
    No idle extension or row lock. A committed revoke before this SELECT must
    be observed. This is the release linearization point, not a network lock.
    """
    _require_clean(session)
    session.rollback()
    try:
        row = session.execute(select(AuthSession.user_id, AuthSession.revoked_at,
            AuthSession.absolute_expires_at, AuthSession.idle_expires_at)
            .join(User, User.id == AuthSession.user_id)
            .where(AuthSession.id == identity.session_id)).one_or_none()
        if not _active(row, utcnow()) or row.user_id != identity.user_id:
            raise InvalidSession
    finally:
        session.rollback()  # Read-only: never renew idle or retain a transaction.
