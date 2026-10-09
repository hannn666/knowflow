import os
from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from project.api.app import create_app
from project.db.business_database import get_database_session, get_database_url
from project.db.business_models import Base


@pytest.fixture
def db_session() -> Iterator[Session]:
    if os.getenv("KNOWFLOW_TEST_POSTGRES") == "1":
        # Use migrated tables. App commits release savepoints, never the outer
        # transaction, which is rolled back after each test.
        engine = create_engine(get_database_url(), hide_parameters=True)
        try:
            with engine.connect() as connection:
                transaction = connection.begin()
                try:
                    with Session(
                        connection, join_transaction_mode="create_savepoint"
                    ) as session:
                        yield session
                finally:
                    transaction.rollback()
        finally:
            engine.dispose()
    else:
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False},
            poolclass=StaticPool, hide_parameters=True,
        )
        try:
            @event.listens_for(engine, "connect")
            def enable_foreign_keys(connection, _):
                cursor = connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

            Base.metadata.create_all(engine)
            with Session(engine) as session:
                yield session
        finally:
            engine.dispose()


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    app = create_app()

    def override_session() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_database_session] = override_session
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def credentials() -> dict[str, str]:
    return {
        "email": f"auth-{uuid4().hex}@example.com",
        "password": "a long registration test phrase",
    }
