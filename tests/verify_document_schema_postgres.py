r"""Verify document migrations and constraints in a newly created PostgreSQL DB.

Run from the repository root:
    .venv\Scripts\python.exe tests/verify_document_schema_postgres.py
The existing business database is never migrated or downgraded by this script.
"""
from pathlib import Path
import os
import sys
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect, text

from project.db.business_database import get_database_url


BASE_REVISION = "a0c08f82e09f"
DOCUMENT_REVISION = "c842a9f713e0"


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    source_url = get_database_url()
    if (source_url.host, source_url.port, source_url.database) != ("127.0.0.1", 5432, "knowflow"):
        raise RuntimeError("Expected local KnowFlow PostgreSQL configuration")
    database_name = f"knowflow_test_documents_{uuid4().hex}"
    test_url = source_url.set(database=database_name)
    admin = create_engine(source_url.set(database="postgres"), isolation_level="AUTOCOMMIT",
                          hide_parameters=True)
    test_engine = None
    created = False
    old_test_mode = os.environ.get("KNOWFLOW_TEST_POSTGRES")
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    try:
        with admin.connect() as connection:
            identity = connection.execute(text(
                "SELECT current_database(), current_user, version()"
            )).one()
            if identity[0] != "postgres" or identity[1] != source_url.username:
                raise RuntimeError("Unexpected maintenance database identity")
            print(f"Instance: 127.0.0.1:5432; maintenance DB: {identity[0]}; {identity[2]}")
            # Name is generated here, never taken from user input or an env var.
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))
            created = True
        print(f"Created disposable database: {database_name}")
        test_engine = create_engine(test_url, hide_parameters=True)
        with test_engine.connect() as connection:
            if connection.scalar(text("SELECT current_database()")) != database_name:
                raise RuntimeError("Unexpected disposable database identity")
        # env.py imports this function, as does pytest's existing conftest.
        # Patch only this verifier process; no production config changes.
        with patch("project.db.business_database.get_database_url", return_value=test_url):
            command.upgrade(config, BASE_REVISION)
            with test_engine.begin() as connection:
                connection.execute(text(
                    "INSERT INTO users (id, email, password_hash) "
                    "VALUES (:id, 'migration-marker@example.com', 'test-only')"
                ), {"id": uuid4().hex})
            command.upgrade(config, "head")
            with test_engine.connect() as connection:
                assert connection.scalar(text("SELECT version_num FROM alembic_version")) == DOCUMENT_REVISION
            assert {"documents", "document_versions", "document_parse_stages"} <= set(inspect(test_engine).get_table_names())
            command.downgrade(config, BASE_REVISION)
            assert {"documents", "document_versions", "document_parse_stages"}.isdisjoint(inspect(test_engine).get_table_names())
            with test_engine.connect() as connection:
                assert connection.scalar(text("SELECT count(*) FROM users WHERE email = 'migration-marker@example.com'")) == 1
            command.upgrade(config, "head")
            command.check(config)
            print("PASS: upgrade -> downgrade to M3 -> re-upgrade; existing marker preserved; alembic check")
            os.environ["KNOWFLOW_TEST_POSTGRES"] = "1"
            return int(pytest.main([
                "-q", "-p", "no:cacheprovider",
                "tests/test_business_models.py", "tests/test_document_versions.py",
                "tests/test_registration.py", "tests/test_login.py",
                "tests/test_knowledge_bases.py", "tests/test_document_api.py",
                "tests/test_document_parser.py", "tests/test_document_parsing_service.py",
                "tests/test_parse_stage.py", "tests/test_parse_artifacts.py", "tests/test_parse_executor.py",
                "tests/test_parse_stage_models.py", "tests/test_parse_stage_concurrency.py",
            ]))
    finally:
        if old_test_mode is None:
            os.environ.pop("KNOWFLOW_TEST_POSTGRES", None)
        else:
            os.environ["KNOWFLOW_TEST_POSTGRES"] = old_test_mode
        if test_engine is not None:
            test_engine.dispose()
        try:
            if created:
                # Only remove the exact randomly named database created above.
                with admin.connect() as connection:
                    connection.execute(text(f'DROP DATABASE "{database_name}"'))
                print(f"Removed disposable database: {database_name}")
        finally:
            admin.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
