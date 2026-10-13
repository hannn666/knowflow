"""Owned loopback FastAPI instance with temporary SQLite; never loads development DB."""
import os
from pathlib import Path
import secrets
import sys
import tempfile
import time

if os.environ.get("KNOWFLOW_M51_FIXTURE") != "1":
    raise RuntimeError("Explicit isolated fixture opt-in is required")
root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root))
os.environ["KNOWFLOW_JWT_SECRET"] = secrets.token_hex(32)
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from project.api.app import create_app
from project.api.auth_service import register_user
from project.api import auth_routes, auth_tokens
from project.db.business_database import get_database_session
from project.db.business_models import Base
import jwt
import uvicorn

parent = root / ".t" / "m51"
parent.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix="http_", dir=parent) as owned:
    engine = create_engine("sqlite:///" + str(Path(owned) / "fixture.sqlite"), connect_args={"check_same_thread": False}, hide_parameters=True)
    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        short_id = None
        for email in ("alice@example.com", "bob@example.com", "short@example.com"):
            user = register_user(session, email, "Fixture-only-password-123")
            if email.startswith("short"):
                short_id = user.id
    original = auth_routes.create_access_token
    def issue(identity):
        value = original(identity)
        if identity != short_id:
            return value
        claims = jwt.decode(value, auth_tokens.get_signing_key(), algorithms=["HS256"], audience="knowflow-api", issuer="knowflow")
        claims["exp"] = int(time.time()) + 4
        return jwt.encode(claims, auth_tokens.get_signing_key(), algorithm="HS256")
    auth_routes.create_access_token = issue  # Test-process only, short expiry fixture.
    app = create_app()
    def session_dependency():
        with Session(engine) as session:
            yield session
    app.dependency_overrides[get_database_session] = session_dependency
    try:
        uvicorn.run(app, host="127.0.0.1", port=8107, log_level="warning", access_log=False)
    finally:
        engine.dispose()
