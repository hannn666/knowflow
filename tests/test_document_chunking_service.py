from dataclasses import asdict, replace
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pymupdf
import pytest
from sqlalchemy import delete, select, update

from project.api.document_chunking_service import generate_document_version_chunks, read_document_version_chunks
from project.api.document_service import DocumentNotFound
from project.core.chunk_artifacts import ChunkArtifactStore
from project.core.document_parser import DocumentVersionSource, parse_pdf_version
from project.core.document_storage import DocumentStorage
from project.core.parse_artifacts import ParseArtifactStore
from project.core.version_chunker import ChunkingConfig, ChunkingError
from project.db.business_models import Document, DocumentParseStage, DocumentVersion, KnowledgeBase, User
from test_document_parsing_service import owned_version


@pytest.fixture
def chunk_service_context(owned_version, db_session):
    user, kb, document, version, storage = owned_version
    source = DocumentVersionSource(kb.id, document.id, version.id, version.original_filename)
    parsed = parse_pdf_version(source, storage)
    attempt = uuid4()
    artifacts = ParseArtifactStore(storage)
    digest = artifacts.publish(artifacts.reserve(source, attempt), parsed)
    db_session.add(DocumentParseStage(document_version_id=version.id, attempt_id=attempt, status="succeeded",
                   finished_at=datetime.now(timezone.utc), page_count=parsed.page_count,
                   has_text=parsed.has_text, result_sha256=digest))
    db_session.commit()
    return (user.id, kb.id, document.id, version.id), storage, artifacts.path_for(source, attempt)


def generate(session, context, config=None):
    ids, storage, _ = context
    return generate_document_version_chunks(session, *ids, storage, config)


def read(session, context, config=None):
    ids, storage, _ = context
    return read_document_version_chunks(session, *ids, storage, config)


def test_generation_read_and_repeat_preserve_all_source_files_and_status(chunk_service_context, db_session, monkeypatch):
    ids, storage, _ = chunk_service_context
    before = {path: path.read_bytes() for path in storage.root.rglob("*") if path.is_file()}
    def forbid_db_write(*args, **kwargs):
        pytest.fail("Chunking must not flush or commit")
    monkeypatch.setattr(db_session, "commit", forbid_db_write)
    monkeypatch.setattr(db_session, "flush", forbid_db_write)
    first = generate(db_session, chunk_service_context)
    assert first.children[0].text == "Public version one"
    assert read(db_session, chunk_service_context) == first
    path = ChunkArtifactStore(storage).path_for(first.binding, first.config)
    fingerprint = path.read_bytes(), path.stat().st_mtime_ns
    from project.api import document_chunking_service as module
    monkeypatch.setattr(module, "chunk_parsed_version", lambda *_: pytest.fail("Successful repeat must use persisted chunks"))
    assert generate(db_session, chunk_service_context) == first
    assert (path.read_bytes(), path.stat().st_mtime_ns) == fingerprint
    assert all(path.read_bytes() == content for path, content in before.items())
    with db_session.no_autoflush:
        assert db_session.scalar(select(DocumentVersion.status).where(DocumentVersion.id == ids[3])) == "pending"
        assert db_session.scalar(select(DocumentParseStage.status).where(DocumentParseStage.document_version_id == ids[3])) == "succeeded"


@pytest.mark.parametrize("operation", [generate_document_version_chunks, read_document_version_chunks])
@pytest.mark.parametrize("mismatch", range(4))
def test_full_ownership_chain_rejects_wrong_ids_before_file_access(chunk_service_context, db_session, monkeypatch, operation, mismatch):
    ids, storage, _ = chunk_service_context
    wrong = list(ids)
    wrong[mismatch] = uuid4()
    monkeypatch.setattr(storage, "path_for", lambda *_: pytest.fail("Unauthorized call must not access files"))
    with pytest.raises(DocumentNotFound):
        operation(db_session, *wrong, storage)


def test_existing_other_users_and_knowledge_bases_rejected(chunk_service_context, db_session):
    ids, storage, _ = chunk_service_context
    other = User(email=f"chunk-other-{uuid4().hex}@example.com", password_hash="test-only")
    db_session.add(other)
    db_session.flush()
    other_kb = KnowledgeBase(owner_id=other.id, name="Other owner")
    own_second_kb = KnowledgeBase(owner_id=ids[0], name="Own second")
    other_document = Document(knowledge_base=other_kb)
    other_version = DocumentVersion(document=other_document, original_filename="shared-name.pdf")
    db_session.add_all([other_version, own_second_kb])
    db_session.commit()
    for wrong in [(other.id, *ids[1:]), (ids[0], own_second_kb.id, *ids[2:]),
                  (ids[0], other_kb.id, other_document.id, other_version.id),
                  (ids[0], ids[1], ids[2], other_version.id)]:
        for operation in (generate_document_version_chunks, read_document_version_chunks):
            with pytest.raises(DocumentNotFound):
                operation(db_session, *wrong, storage)
    assert not list(storage.root.rglob("chunks.json"))


@pytest.mark.parametrize("phase", ["absent", "running", "failed"])
def test_only_current_successful_parse_can_generate(chunk_service_context, db_session, phase):
    ids, storage, _ = chunk_service_context
    if phase == "absent":
        db_session.execute(delete(DocumentParseStage).where(DocumentParseStage.document_version_id == ids[3]))
    else:
        values = dict(status=phase, page_count=None, has_text=None, result_sha256=None,
                      finished_at=None if phase == "running" else datetime.now(timezone.utc),
                      error_code=None if phase == "running" else "invalid_pdf")
        db_session.execute(update(DocumentParseStage).where(DocumentParseStage.document_version_id == ids[3]).values(**values))
    db_session.commit()
    for operation in (generate, read):
        with pytest.raises(ChunkingError) as error:
            operation(db_session, chunk_service_context)
        assert error.value.code == "parse_not_succeeded"
    assert not list(storage.root.rglob("chunks.json"))


@pytest.mark.parametrize("fault", ["missing", "corrupt", "digest", "summary"])
def test_invalid_parse_artifacts_rejected_without_state_mutation(chunk_service_context, db_session, fault):
    ids, storage, path = chunk_service_context
    if fault == "missing":
        path.unlink()
    if fault == "corrupt":
        path.write_bytes(b"{partial")
    if fault in {"digest", "summary"}:
        values = {"result_sha256": "b" * 64} if fault == "digest" else {"page_count": 3}
        db_session.execute(update(DocumentParseStage).where(DocumentParseStage.document_version_id == ids[3]).values(**values))
        db_session.commit()
    with pytest.raises(ChunkingError) as error:
        generate(db_session, chunk_service_context)
    assert error.value.code.startswith("parse_artifact_")
    assert str(storage.root) not in str(error.value)
    assert not list(storage.root.rglob("chunks.json"))
    assert db_session.scalar(select(DocumentParseStage.status).where(DocumentParseStage.document_version_id == ids[3])) == "succeeded"


def test_dirty_orm_metadata_is_not_flushed_or_used(chunk_service_context, db_session, monkeypatch):
    ids, _, _ = chunk_service_context
    item = db_session.get(DocumentVersion, ids[3])
    item.original_filename = "uncommitted.pdf"
    monkeypatch.setattr(db_session, "flush", lambda *_: pytest.fail("Unrelated edits must not flush"))
    result = generate(db_session, chunk_service_context)
    assert result.binding.source.original_filename == "shared-name.pdf"
    assert item in db_session.dirty


def test_different_versions_and_configuration_never_overwrite(chunk_service_context, db_session):
    ids, storage, _ = chunk_service_context
    first = generate(db_session, chunk_service_context)
    second_version = DocumentVersion(document_id=ids[2], original_filename="shared-name.pdf")
    db_session.add(second_version)
    db_session.commit()
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), "Second public version")
        payload = pdf.tobytes()
    storage.save(BytesIO(payload), ids[1], ids[2], second_version.id)
    source = DocumentVersionSource(ids[1], ids[2], second_version.id, second_version.original_filename)
    parsed = parse_pdf_version(source, storage)
    artifacts = ParseArtifactStore(storage)
    attempt = uuid4()
    digest = artifacts.publish(artifacts.reserve(source, attempt), parsed)
    db_session.add(DocumentParseStage(document_version_id=second_version.id, attempt_id=attempt, status="succeeded",
                   finished_at=datetime.now(timezone.utc), page_count=parsed.page_count, has_text=parsed.has_text, result_sha256=digest))
    db_session.commit()
    second = generate_document_version_chunks(db_session, ids[0], ids[1], ids[2], second_version.id, storage)
    assert second.children[0].text == "Second public version"
    assert {item.chunk_id for item in first.children}.isdisjoint(item.chunk_id for item in second.children)
    assert read(db_session, chunk_service_context) == first
    configured = generate(db_session, chunk_service_context, replace(ChunkingConfig(), child_chunk_overlap=50))
    assert configured != first and len(list(storage.root.rglob("chunks.json"))) == 3


def test_parse_attempt_replacement_invalidates_old_chunk_cache(chunk_service_context, db_session):
    ids, storage, path = chunk_service_context
    first = generate(db_session, chunk_service_context)
    source = first.binding.source
    artifacts = ParseArtifactStore(storage)
    parsed = artifacts.read(source, first.binding.attempt_id, first.binding.sha256)
    new_attempt = uuid4()
    digest = artifacts.publish(artifacts.reserve(source, new_attempt), parsed)
    db_session.execute(update(DocumentParseStage).where(DocumentParseStage.document_version_id == ids[3]).values(
                       attempt_id=new_attempt, result_sha256=digest))
    db_session.commit()
    with pytest.raises(ChunkingError) as error:
        read(db_session, chunk_service_context)
    assert error.value.code == "chunk_artifact_missing"
    second = generate(db_session, chunk_service_context)
    assert second.binding.attempt_id == new_attempt and second.children[0].chunk_id != first.children[0].chunk_id
    assert len(list(storage.root.rglob("chunks.json"))) == 2
    assert path.exists()


def test_parse_change_during_generation_is_rejected_before_publication(chunk_service_context, db_session, monkeypatch):
    from project.api import document_chunking_service as module
    ids, storage, _ = chunk_service_context
    actual = module.chunk_parsed_version
    def invalidate_during_chunking(*args):
        result = actual(*args)
        db_session.execute(update(DocumentParseStage).where(DocumentParseStage.document_version_id == ids[3]).values(
            status="failed", page_count=None, has_text=None, result_sha256=None,
            finished_at=datetime.now(timezone.utc), error_code="artifact_unavailable"))
        db_session.commit()
        return result
    monkeypatch.setattr(module, "chunk_parsed_version", invalidate_during_chunking)
    with pytest.raises(ChunkingError) as error:
        generate(db_session, chunk_service_context)
    assert error.value.code == "parse_not_succeeded"
    assert not list(storage.root.rglob("chunks.json"))


def test_failure_to_save_chunks_does_not_alter_parse_or_original(chunk_service_context, db_session, monkeypatch):
    import os
    ids, storage, parse_path = chunk_service_context
    original = storage.path_for(*ids[1:]).read_bytes()
    saved_parse = parse_path.read_bytes()
    def deny_link(*args):
        raise OSError("private file path sentinel")
    monkeypatch.setattr(os, "link", deny_link)
    with pytest.raises(ChunkingError) as error:
        generate(db_session, chunk_service_context)
    assert error.value.code == "chunk_artifact_write_failed"
    assert storage.path_for(*ids[1:]).read_bytes() == original and parse_path.read_bytes() == saved_parse
    assert not list(storage.root.rglob("chunks.json")) and not list(storage.root.rglob("*.part"))
    assert db_session.scalar(select(DocumentParseStage.status).where(DocumentParseStage.document_version_id == ids[3])) == "succeeded"


def test_internal_chunk_services_do_not_create_public_or_rag_entry_points():
    import ast
    from project.api.app import create_app
    root = Path(__file__).resolve().parents[1]
    names = ["project/api/document_chunking_service.py", "project/core/version_chunker.py", "project/core/chunk_artifacts.py"]
    for name in names:
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        imports += [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
        assert not any("qdrant" in item or "rag_agent" in item or "document_manager" in item or "embedding" in item for item in imports)
    assert not any("chunk" in getattr(route, "path", "") for route in create_app().routes)


def test_real_uploaded_chinese_and_english_pdf_parse_then_chunk(client, db_session, tmp_path, monkeypatch):
    from project.api import auth_tokens
    from project.core.document_storage import get_document_storage
    from project.core.parse_executor import BoundedParseExecutor, get_parse_executor
    monkeypatch.setattr(auth_tokens, "load_dotenv", lambda *_: None)
    monkeypatch.setenv("KNOWFLOW_JWT_SECRET", "ab" * 32)
    storage = DocumentStorage(tmp_path)
    client.app.dependency_overrides[get_document_storage] = lambda: storage
    client.app.dependency_overrides[get_parse_executor] = lambda: BoundedParseExecutor()
    credentials = {"email": f"chunk-pdf-{uuid4().hex}@example.com", "password": "test-only PDF chunk integration"}
    user = client.post("/auth/register", json=credentials).json()
    login = client.post("/auth/login", json=credentials)
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    kb = client.post("/knowledge-bases", headers=headers, json={"name": "Chunking PDF"}).json()
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), "English public PDF page")
        pdf.new_page()
        pdf.new_page().insert_text((72, 72), "中文公开测试页面", fontname="china-s")
        payload = pdf.tobytes()
    uploaded = client.post(f"/knowledge-bases/{kb['id']}/documents", headers=headers,
                          files={"file": ("public.pdf", payload, "application/pdf")})
    assert uploaded.status_code == 201
    item = uploaded.json()
    path = f"/knowledge-bases/{kb['id']}/documents/{item['document_id']}/versions/{item['document_version_id']}"
    assert client.post(path + "/parse", headers=headers).json()["parse_status"] == "succeeded"
    ids = tuple(UUID(value) for value in (user["id"], kb["id"], item["document_id"], item["document_version_id"]))
    result = generate_document_version_chunks(db_session, *ids, storage)
    assert result.page_count == 3 and result.blank_page_numbers == (2,)
    assert [item.page_number for item in result.children] == [1, 3]
    assert result.children[0].text == "English public PDF page"
    assert "".join(result.children[1].text.split()) == "中文公开测试页面"
    assert read_document_version_chunks(db_session, *ids, DocumentStorage(tmp_path)) == result
    assert client.get(path, headers=headers).json()["status"] == "pending"


@pytest.mark.parametrize("status", ["pending", "processing", "failed", "ready"])
def test_chunking_never_redefines_overall_version_status(chunk_service_context, db_session, status):
    ids, _, _ = chunk_service_context
    db_session.execute(update(DocumentVersion).where(DocumentVersion.id == ids[3]).values(status=status))
    db_session.commit()
    generate(db_session, chunk_service_context)
    assert db_session.scalar(select(DocumentVersion.status).where(DocumentVersion.id == ids[3])) == status


@pytest.mark.parametrize("kind", ["same_kb_document", "own_other_kb", "other_user"])
def test_same_filename_isolation_across_documents_kbs_and_owners(chunk_service_context, db_session, kind):
    ids, storage, _ = chunk_service_context
    first = generate(db_session, chunk_service_context)
    owner_id = ids[0]
    kb_id = ids[1]
    if kind == "other_user":
        owner = User(email=f"chunk-isolation-{uuid4().hex}@example.com", password_hash="test-only")
        db_session.add(owner)
        db_session.flush()
        owner_id = owner.id
    if kind != "same_kb_document":
        kb = KnowledgeBase(owner_id=owner_id, name="Isolated chunks")
        db_session.add(kb)
        db_session.flush()
        kb_id = kb.id
    document = Document(knowledge_base_id=kb_id)
    item = DocumentVersion(document=document, original_filename="shared-name.pdf")
    db_session.add(item)
    db_session.commit()
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), "Different private test source")
        payload = pdf.tobytes()
    storage.save(BytesIO(payload), kb_id, document.id, item.id)
    source = DocumentVersionSource(kb_id, document.id, item.id, item.original_filename)
    parsed = parse_pdf_version(source, storage)
    artifacts = ParseArtifactStore(storage)
    attempt = uuid4()
    digest = artifacts.publish(artifacts.reserve(source, attempt), parsed)
    db_session.add(DocumentParseStage(document_version_id=item.id, attempt_id=attempt, status="succeeded",
                   finished_at=datetime.now(timezone.utc), page_count=parsed.page_count, has_text=parsed.has_text, result_sha256=digest))
    db_session.commit()
    second = generate_document_version_chunks(db_session, owner_id, kb_id, document.id, item.id, storage)
    assert second.children[0].text == "Different private test source"
    assert second.binding.source != first.binding.source
    assert {item.chunk_id for item in second.parents + second.children}.isdisjoint(
        item.chunk_id for item in first.parents + first.children)
    assert read(db_session, chunk_service_context) == first
    assert len(list(storage.root.rglob("chunks.json"))) == 2


def test_current_successful_attempt_change_cannot_publish_stale_chunks(chunk_service_context, db_session, monkeypatch):
    from project.api import document_chunking_service as module
    ids, storage, _ = chunk_service_context
    actual = module.chunk_parsed_version
    def replace_attempt(*args):
        result = actual(*args)
        artifacts = ParseArtifactStore(storage)
        parsed = artifacts.read(result.binding.source, result.binding.attempt_id, result.binding.sha256)
        attempt = uuid4()
        digest = artifacts.publish(artifacts.reserve(result.binding.source, attempt), parsed)
        db_session.execute(update(DocumentParseStage).where(DocumentParseStage.document_version_id == ids[3]).values(
            attempt_id=attempt, result_sha256=digest))
        db_session.commit()
        return result
    monkeypatch.setattr(module, "chunk_parsed_version", replace_attempt)
    with pytest.raises(ChunkingError) as error:
        generate(db_session, chunk_service_context)
    assert error.value.code == "parse_input_changed"
    assert not list(storage.root.rglob("chunks.json"))


def test_invalidation_after_publication_retains_cache_but_never_returns_success(chunk_service_context, db_session, monkeypatch):
    ids, storage, _ = chunk_service_context
    actual = ChunkArtifactStore.publish
    def invalidate_after_publication(self, result):
        value = actual(self, result)
        db_session.execute(update(DocumentParseStage).where(DocumentParseStage.document_version_id == ids[3]).values(
            status="failed", page_count=None, has_text=None, result_sha256=None,
            finished_at=datetime.now(timezone.utc), error_code="artifact_unavailable"))
        db_session.commit()
        return value
    monkeypatch.setattr(ChunkArtifactStore, "publish", invalidate_after_publication)
    with pytest.raises(ChunkingError) as error:
        generate(db_session, chunk_service_context)
    assert error.value.code == "parse_not_succeeded"
    assert len(list(storage.root.rglob("chunks.json"))) == 1
    with pytest.raises(ChunkingError) as error:
        read(db_session, chunk_service_context)
    assert error.value.code == "parse_not_succeeded"
