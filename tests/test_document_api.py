from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from project.api import auth_tokens
from project.core.document_storage import DocumentStorage, get_document_storage
from project.db.business_models import Document, DocumentVersion


PDF = b"%PDF-1.7\npublic test fixture only"


@pytest.fixture(autouse=True)
def signing_key(monkeypatch):
    monkeypatch.setattr(auth_tokens, "load_dotenv", lambda *_: None)
    monkeypatch.setenv("KNOWFLOW_JWT_SECRET", "ab" * 32)


@pytest.fixture
def upload_context(client, tmp_path):
    storage = DocumentStorage(tmp_path)
    client.app.dependency_overrides[get_document_storage] = lambda: storage
    accounts = []
    for _ in range(2):
        credentials = {"email": f"upload-{uuid4().hex}@example.com", "password": "test-only upload password phrase"}
        assert client.post("/auth/register", json=credentials).status_code == 201
        token = client.post("/auth/login", json=credentials).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        kb = client.post("/knowledge-bases", headers=headers, json={"name": "Upload tests"}).json()["id"]
        accounts.append((headers, kb))
    return client, accounts, storage


def post(client, headers, kb, name="manual.pdf", content=PDF, mime="application/pdf", **kwargs):
    return client.post(f"/knowledge-bases/{kb}/documents", headers=headers,
                       files={"file": (name, content, mime)}, **kwargs)


def url(kb, document, version):
    return f"/knowledge-bases/{kb}/documents/{document}/versions/{version}"


def test_upload_and_query_persist_pending(upload_context, db_session):
    client, accounts, storage = upload_context
    headers, kb = accounts[0]
    response = post(client, headers, kb)
    assert response.status_code == 201
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert set(body) == {"knowledge_base_id", "document_id", "document_version_id", "original_filename", "status", "created_at"}
    assert body["status"] == "pending"
    assert body["knowledge_base_id"] == kb
    assert body["original_filename"] == "manual.pdf"
    document = db_session.get(Document, UUID(body["document_id"]))
    version = db_session.get(DocumentVersion, UUID(body["document_version_id"]))
    assert str(document.knowledge_base_id) == kb
    assert version.document_id == document.id
    assert storage.path_for(UUID(kb), document.id, version.id).read_bytes() == PDF
    assert not list(storage.root.rglob("*.part"))
    detail = client.get(url(kb, document.id, version.id), headers=headers)
    assert detail.status_code == 200
    assert detail.json() == body
    assert detail.headers["Cache-Control"] == "no-store"


def test_identical_filenames_create_independent_documents(upload_context):
    client, accounts, storage = upload_context
    headers, kb = accounts[0]
    first = post(client, headers, kb).json()
    second = post(client, headers, kb, content=PDF + b"second").json()
    assert first["document_id"] != second["document_id"]
    assert first["document_version_id"] != second["document_version_id"]
    assert len(list(storage.root.rglob("source.pdf"))) == 2


def test_cross_user_and_cross_knowledge_base_ids_rejected(upload_context, db_session):
    client, accounts, storage = upload_context
    headers, kb = accounts[0]
    other_headers, other_kb = accounts[1]
    item = post(client, headers, kb).json()
    own_second_kb = client.post("/knowledge-bases", headers=headers, json={"name": "second"}).json()["id"]
    missing = client.get(url(kb, uuid4(), uuid4()), headers=headers)
    for request_headers, request_kb, document_id, version_id in [
        (other_headers, kb, item["document_id"], item["document_version_id"]),
        (headers, other_kb, item["document_id"], item["document_version_id"]),
        (headers, own_second_kb, item["document_id"], item["document_version_id"]),
        (headers, kb, uuid4(), item["document_version_id"]),
        (headers, kb, item["document_id"], uuid4()),
    ]:
        response = client.get(url(request_kb, document_id, version_id), headers=request_headers)
        assert response.status_code == 404
        assert response.json() == missing.json()
    for request_headers, request_kb in [(other_headers, kb), (headers, other_kb), (headers, str(uuid4()))]:
        assert post(client, request_headers, request_kb).status_code == 404
    assert db_session.scalar(select(func.count()).select_from(Document)) == 1
    assert len(list(storage.root.rglob("source.pdf"))) == 1


@pytest.mark.parametrize("authorization", [None, "Bearer invalid-token"])
def test_upload_and_query_require_auth(upload_context, db_session, authorization):
    client, accounts, storage = upload_context
    headers = {} if authorization is None else {"Authorization": authorization}
    kb = accounts[0][1]
    assert post(client, headers, kb).status_code == 401
    assert client.get(url(kb, uuid4(), uuid4()), headers=headers).status_code == 401
    assert db_session.scalar(select(func.count()).select_from(Document)) == 0
    assert not list(storage.root.rglob("source.pdf"))


@pytest.mark.parametrize("name,content,mime,code", [
    ("manual.pdf", b"", "application/pdf", 422),
    ("manual.txt", PDF, "application/pdf", 415),
    ("manual.pdf", b"fake PDF", "application/pdf", 415),
    ("manual.pdf", PDF, "text/plain", 415),
    ("../manual.pdf", PDF, "application/pdf", 422),
    ("x" * 252 + ".pdf", PDF, "application/pdf", 422),
])
def test_invalid_upload_leaves_no_metadata_or_file(upload_context, db_session, name, content, mime, code):
    client, accounts, storage = upload_context
    headers, kb = accounts[0]
    response = post(client, headers, kb, name, content, mime)
    assert response.status_code == code
    assert str(storage.root) not in response.text
    assert db_session.scalar(select(func.count()).select_from(Document)) == 0
    assert db_session.scalar(select(func.count()).select_from(DocumentVersion)) == 0
    assert not list(storage.root.rglob("*.pdf"))
    assert not list(storage.root.rglob("*.part"))


def test_filename_255_characters_and_octet_stream_accepted(upload_context):
    client, accounts, _ = upload_context
    headers, kb = accounts[0]
    response = post(client, headers, kb, "x" * 251 + ".pdf", mime="application/octet-stream")
    assert response.status_code == 201
    assert len(response.json()["original_filename"]) == 255


@pytest.mark.parametrize("extra", [{"owner_id": "forged"}, {"status": "ready"}, {"path": "outside"}])
def test_extra_form_fields_rejected(upload_context, db_session, extra):
    client, accounts, storage = upload_context
    headers, kb = accounts[0]
    assert post(client, headers, kb, data=extra).status_code == 422
    assert db_session.scalar(select(func.count()).select_from(Document)) == 0
    assert not list(storage.root.rglob("*.pdf"))


def test_multiple_files_rejected(upload_context):
    client, accounts, storage = upload_context
    headers, kb = accounts[0]
    response = client.post(f"/knowledge-bases/{kb}/documents", headers=headers,
                           files=[("file", ("one.pdf", PDF, "application/pdf")),
                                  ("file", ("two.pdf", PDF, "application/pdf"))])
    assert response.status_code == 422
    assert not list(storage.root.rglob("*.pdf"))


def test_file_limit_beyond_valid_header_rejected(upload_context, db_session):
    client, accounts, storage = upload_context
    headers, kb = accounts[0]
    response = post(client, headers, kb, content=PDF + b"x" * (10 * 1024 * 1024 + 1 - len(PDF)))
    assert response.status_code == 413
    assert db_session.scalar(select(func.count()).select_from(Document)) == 0
    assert not list(storage.root.rglob("*.pdf"))


def test_known_commit_rejection_cleans_only_own_file(upload_context, db_session, monkeypatch):
    client, accounts, storage = upload_context
    headers, kb = accounts[0]
    first = post(client, headers, kb).json()
    original = storage.path_for(UUID(kb), UUID(first["document_id"]), UUID(first["document_version_id"]))
    def reject_commit():
        raise IntegrityError("test-only commit", {}, RuntimeError("rejected"))
    monkeypatch.setattr(db_session, "commit", reject_commit)
    response = post(client, headers, kb)
    assert response.status_code == 503
    assert original.read_bytes() == PDF
    assert len(list(storage.root.rglob("source.pdf"))) == 1
    assert db_session.scalar(select(func.count()).select_from(Document)) == 1


def test_exception_after_successful_commit_keeps_referenced_file(upload_context, db_session, monkeypatch):
    client, accounts, storage = upload_context
    headers, kb = accounts[0]
    actual_commit = db_session.commit
    def ambiguous_commit():
        actual_commit()
        raise RuntimeError("lost acknowledgement")
    monkeypatch.setattr(db_session, "commit", ambiguous_commit)
    assert post(client, headers, kb).status_code == 503
    saved = db_session.scalar(select(DocumentVersion))
    assert saved is not None
    assert storage.path_for(UUID(kb), saved.document_id, saved.id).read_bytes() == PDF


def test_disk_failure_rolls_back_metadata(upload_context, db_session, monkeypatch):
    client, accounts, storage = upload_context
    headers, kb = accounts[0]
    def fail_save(*args):
        raise OSError("private path must not be returned")
    monkeypatch.setattr(storage, "save", fail_save)
    response = post(client, headers, kb)
    assert response.status_code == 503
    assert "private path" not in response.text
    assert db_session.scalar(select(func.count()).select_from(Document)) == 0
    assert not list(storage.root.rglob("source.pdf"))


def test_multipart_normalized_windows_filename_is_only_display_information(upload_context):
    client, accounts, storage = upload_context
    headers, kb = accounts[0]
    response = post(client, headers, kb, "C:\\manual.pdf")
    assert response.status_code == 201
    assert response.json()["original_filename"] == "manual.pdf"
    saved = list(storage.root.rglob("source.pdf"))
    assert len(saved) == 1
    assert "manual" not in str(saved[0].relative_to(storage.root))

def test_upload_validation_does_not_echo_submitted_fields(upload_context):
    client, accounts, _ = upload_context
    headers, kb = accounts[0]
    response = client.post(f"/knowledge-bases/{kb}/documents", headers=headers,
                           files={"file": (None, "private-marker-field")})
    assert response.status_code == 422
    assert "private-marker-field" not in response.text
    assert all(set(error) == {"type", "loc", "msg"} for error in response.json()["detail"])


def test_unknown_commit_failure_retains_orphan_for_reconciliation(upload_context, db_session, monkeypatch):
    client, accounts, storage = upload_context
    headers, kb = accounts[0]
    def fail_commit():
        raise RuntimeError("connection result unknown")
    monkeypatch.setattr(db_session, "commit", fail_commit)
    assert post(client, headers, kb).status_code == 503
    assert db_session.scalar(select(func.count()).select_from(Document)) == 0
    assert len(list(storage.root.rglob("source.pdf"))) == 1


def test_ambiguous_commit_failure_logs_without_database_reads(upload_context, db_session, monkeypatch, caplog):
    from sqlalchemy import event
    client, accounts, storage = upload_context
    headers, kb = accounts[0]
    actual_commit = db_session.commit
    failed = False
    reads_after_failure = []

    def commit_without_acknowledgement():
        nonlocal failed
        actual_commit()
        db_session.expire_all()
        failed = True
        raise RuntimeError("commit acknowledgement lost")

    def deny_database_reads(execute_state):
        if failed:
            reads_after_failure.append(execute_state)
            raise RuntimeError("database connection unavailable")

    event.listen(db_session, "do_orm_execute", deny_database_reads)
    monkeypatch.setattr(db_session, "commit", commit_without_acknowledgement)
    try:
        assert post(client, headers, kb).status_code == 503
        assert not reads_after_failure
        assert "original file retained" in caplog.text
        assert len(list(storage.root.rglob("source.pdf"))) == 1
    finally:
        event.remove(db_session, "do_orm_execute", deny_database_reads)
