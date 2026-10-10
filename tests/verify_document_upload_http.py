"""Real HTTP smoke test against the migrated local development database.

Creates two uniquely named test accounts and only removes rows owned by those
accounts. Original files are confined to a TemporaryDirectory. No migration.
"""
from pathlib import Path
import os
import secrets
import socket
import sys
import tempfile
from threading import Thread
import time
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import pymupdf
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session
import uvicorn

from project.api.app import create_app
from project.db.business_database import get_database_engine, get_database_url
from project.db.business_models import Document, DocumentVersion, KnowledgeBase, User


def main() -> None:
    url = get_database_url()
    if (url.host, url.port, url.database) != ("127.0.0.1", 5432, "knowflow"):
        raise RuntimeError("Expected local development database")
    engine = get_database_engine()
    with engine.connect() as connection:
        identity = connection.execute(text(
            "SELECT current_database(), current_user, version_num FROM alembic_version"
        )).one()
        if identity[:2] != ("knowflow", "knowflow") or identity[2] not in {"b71d4a09e6c2", "c842a9f713e0"}:
            raise RuntimeError("Development database identity or migration revision mismatch")
    print("Confirmed local development database revision: " + identity[2])
    emails = [f"http-upload-{uuid4().hex}@example.com" for _ in range(2)]
    created_user_ids = []
    directory = tempfile.TemporaryDirectory(prefix="knowflow-http-upload-")
    root = Path(directory.name).resolve()
    if not root.is_relative_to(Path(tempfile.gettempdir()).resolve()):
        raise RuntimeError("Unexpected temporary upload root")
    old_root = os.environ.get("KNOWFLOW_UPLOAD_ROOT")
    os.environ["KNOWFLOW_UPLOAD_ROOT"] = str(root)
    socket_handle = socket.socket()
    socket_handle.bind(("127.0.0.1", 0))
    port = socket_handle.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=port,
                                         log_level="error", access_log=False))
    thread = Thread(target=server.run, kwargs={"sockets": [socket_handle]}, daemon=True)
    try:
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        if not server.started:
            raise RuntimeError("HTTP server did not start")
        with pymupdf.open() as pdf:
            pdf.new_page().insert_text((72, 72), "KnowFlow public HTTP test document")
            payload = pdf.tobytes()
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=15) as client:
            assert client.get("/health").status_code == 200
            headers = []
            for email in emails:
                credentials = {"email": email, "password": secrets.token_urlsafe(24)}
                registration = client.post("/auth/register", json=credentials)
                if registration.status_code != 201:
                    raise RuntimeError("Unable to create isolated HTTP test account")
                created_user_ids.append(UUID(registration.json()["id"]))
                login = client.post("/auth/login", json=credentials)
                assert login.status_code == 200
                headers.append({"Authorization": f"Bearer {login.json()['access_token']}"})
            knowledge_bases = []
            for authorization in headers:
                response = client.post("/knowledge-bases", headers=authorization, json={"name": "HTTP upload verification"})
                assert response.status_code == 201
                knowledge_bases.append(response.json()["id"])
            upload_url = f"/knowledge-bases/{knowledge_bases[0]}/documents"
            upload = client.post(upload_url, headers=headers[0], files={"file": ("manual.pdf", payload, "application/pdf")})
            assert upload.status_code == 201
            item = upload.json()
            assert item["status"] == "pending"
            path = root / knowledge_bases[0] / item["document_id"] / item["document_version_id"] / "source.pdf"
            assert path.read_bytes() == payload
            detail_url = upload_url + f"/{item['document_id']}/versions/{item['document_version_id']}"
            detail = client.get(detail_url, headers=headers[0])
            assert detail.status_code == 200 and detail.json() == item
            assert client.get(detail_url, headers=headers[1]).status_code == 404
            assert client.get(detail_url).status_code == 401
            assert client.post(upload_url, headers=headers[1], files={"file": ("manual.pdf", payload, "application/pdf")}).status_code == 404
            wrong_kb_url = detail_url.replace(knowledge_bases[0], knowledge_bases[1])
            assert client.get(wrong_kb_url, headers=headers[0]).status_code == 404
            repeated = client.post(upload_url, headers=headers[0], files={"file": ("manual.pdf", payload, "application/pdf")})
            assert repeated.status_code == 201 and repeated.json()["document_id"] != item["document_id"]
            assert len(list(root.rglob("source.pdf"))) == 2
            print("PASS: real HTTP health/register/login/KB/upload/query, cross-user/KB denial, same-name uploads, exact stored bytes")
    finally:
        server.should_exit = True
        if thread.ident is not None:
            thread.join(timeout=10)
        socket_handle.close()
        if thread.is_alive():
            raise RuntimeError("HTTP server did not stop; preserve temporary uploads for inspection")
        # Exact random account identifiers only, never a global DELETE.
        with Session(engine) as session:
            user_ids = list(session.scalars(select(User.id).where(User.id.in_(created_user_ids), User.email.in_(emails))))
            kb_ids = list(session.scalars(select(KnowledgeBase.id).where(KnowledgeBase.owner_id.in_(user_ids))))
            document_ids = list(session.scalars(select(Document.id).where(Document.knowledge_base_id.in_(kb_ids))))
            session.execute(delete(DocumentVersion).where(DocumentVersion.document_id.in_(document_ids)))
            session.execute(delete(Document).where(Document.id.in_(document_ids)))
            session.execute(delete(KnowledgeBase).where(KnowledgeBase.id.in_(kb_ids)))
            session.execute(delete(User).where(User.id.in_(user_ids)))
            session.commit()
            assert session.scalar(select(User.id).where(User.id.in_(created_user_ids))) is None
        print("Removed only successfully created test accounts and their KB/document/version rows")
        if old_root is None:
            os.environ.pop("KNOWFLOW_UPLOAD_ROOT", None)
        else:
            os.environ["KNOWFLOW_UPLOAD_ROOT"] = old_root
        directory.cleanup()
        print("Removed isolated HTTP upload files; existing business data untouched")


if __name__ == "__main__":
    main()
