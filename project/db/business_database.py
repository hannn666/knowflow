import os
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import URL, Engine
from sqlalchemy.orm import Session


_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def get_database_url() -> URL:
    load_dotenv(_PROJECT_ROOT / ".env")
    password = os.getenv("POSTGRES_PASSWORD")
    if not password:
        raise RuntimeError("POSTGRES_PASSWORD must be set in .env")

    return URL.create(
        "postgresql+psycopg",
        username="knowflow",
        password=password,
        host="127.0.0.1",
        port=5432,
        database="knowflow",
    )


@lru_cache(maxsize=1)
def get_database_engine() -> Engine:
    # Construct lazily so /health and /chat do not require database settings.
    return create_engine(
        get_database_url(), pool_pre_ping=True, hide_parameters=True
    )


def get_database_session() -> Iterator[Session]:
    # Each request gets its own unit of work, closed even when it fails.
    with Session(get_database_engine()) as session:
        yield session
