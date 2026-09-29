import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.engine import URL


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
