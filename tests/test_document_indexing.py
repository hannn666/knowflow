from dataclasses import replace
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pymupdf
import pytest
from qdrant_client import QdrantClient, models
from sqlalchemy import select, update

from project.api import document_indexing_service as service
from project.api.document_chunking_service import generate_document_version_chunks
from project.api.document_service import DocumentNotFound
from project.core.chunk_artifacts import ChunkArtifactStore
from project.core.index_models import EmbeddingSpec, IndexSettings, IndexingError
from project.core.version_index import VECTOR_NAME, VersionIndex, collection_for
from project.db.business_models import DocumentVersion, DocumentParseStage
from test_document_chunking_service import chunk_service_context
from test_document_parsing_service import owned_version


class SyntheticEncoder:
    """Deterministic test vectors; not evidence of a real model inference."""
    def __init__(self):
        self.calls = 0
        self.failure_at = None
        self.override = None

    def embed_documents(self, texts):
        self.calls += 1
        if self.failure_at == self.calls:
            raise RuntimeError("private text/path failure sentinel")
        return self.override if self.override is not None else [[1.0 + len(text) % 7, 2.0, 3.0] for text in texts]


@pytest.fixture
def indexing_context(chunk_service_context, db_session):
    ids, storage, parse_path = chunk_service_context
    chunked = generate_document_version_chunks(db_session, *ids, storage)
    backend = VersionIndex(IndexSettings(batch_size=1), client=QdrantClient(":memory:"))
    encoder = SyntheticEncoder()
    spec = EmbeddingSpec("test-only-synthetic", "synthetic-v1", 3)
    try:
        yield ids, storage, chunked, backend, encoder, spec, parse_path
    finally:
        backend.close()


def index(session, context):
    ids, storage, _, backend, encoder, spec, _ = context
    return service.index_document_version(session, *ids, storage, index=backend, encoder=encoder, spec=spec)


def test_single_index_metadata_parent_vector_readback_and_idempotence(indexing_context, db_session, monkeypatch):
    ids, storage, chunks, backend, encoder, spec, _ = indexing_context
    def forbid_write(*args):
        pytest.fail("Indexing must not commit or flush")
    monkeypatch.setattr(db_session, "commit", forbid_write)
    monkeypatch.setattr(db_session, "flush", forbid_write)
    first = index(db_session, indexing_context)
    assert first.verified and first.point_count == len(chunks.children)
    assert first.parent_count == len(chunks.parents)
    assert first.collection_name == "knowflow_kb_" + ids[1].hex
    calls = encoder.calls
    assert index(db_session, indexing_context) == first and encoder.calls == calls
    points = backend._client.retrieve(first.collection_name, ids=[str(item.chunk_id) for item in chunks.children], with_vectors=True)
    assert len(points) == 1
    metadata = points[0].payload["metadata"]
    assert metadata["knowledge_base_id"] == str(ids[1])
    assert metadata["document_id"] == str(ids[2]) and metadata["document_version_id"] == str(ids[3])
    assert metadata["parent_id"] == str(chunks.parents[0].chunk_id)
    assert metadata["chunk_id"] == str(chunks.children[0].chunk_id) and metadata["page_number"] == 1
    assert metadata["parse_attempt_id"] == str(chunks.binding.attempt_id)
    assert metadata["parse_sha256"] == chunks.binding.sha256
    assert metadata["chunking_fingerprint"] == chunks.binding.fingerprint(chunks.config)
    assert metadata["embedding_fingerprint"] == spec.fingerprint
    assert len(points[0].vector[VECTOR_NAME]) == spec.dimension
    assert points[0].payload["page_content"] == "Public version one"
    with db_session.no_autoflush:
        assert db_session.scalar(select(DocumentVersion.status).where(DocumentVersion.id == ids[3])) == "pending"
        assert db_session.scalar(select(DocumentParseStage.status).where(DocumentParseStage.document_version_id == ids[3])) == "succeeded"
    assert len(list(storage.root.rglob("chunks.json"))) == 1


@pytest.mark.parametrize("position", range(4))
def test_wrong_identity_chain_never_reaches_model_or_qdrant(indexing_context, db_session, monkeypatch, position):
    ids, storage, _, backend, _, _, _ = indexing_context
    wrong = list(ids)
    wrong[position] = uuid4()
    monkeypatch.setattr(service, "get_index_embeddings", lambda: pytest.fail("Unauthorized model load"))
    monkeypatch.setattr(backend, "missing", lambda *_: pytest.fail("Unauthorized backend access"))
    with pytest.raises(DocumentNotFound):
        service.index_document_version(db_session, *wrong, storage, index=backend)


@pytest.mark.parametrize("artifact", ["parse_missing", "parse_corrupt", "chunks_missing", "chunks_corrupt"])
def test_bad_source_rejected_before_backend_and_encoder(indexing_context, db_session, monkeypatch, artifact):
    _, storage, chunked, backend, encoder, _, parse_path = indexing_context
    path = parse_path if artifact.startswith("parse") else ChunkArtifactStore(storage).path_for(chunked.binding, chunked.config)
    if artifact.endswith("missing"):
        path.unlink()
    else:
        path.write_bytes(b"{partial")
    monkeypatch.setattr(backend, "missing", lambda *_: pytest.fail("Invalid artifact reached Qdrant"))
    with pytest.raises(IndexingError) as error:
        index(db_session, indexing_context)
    assert error.value.code.startswith("source_") and encoder.calls == 0
    assert str(storage.root) not in str(error.value)


@pytest.mark.parametrize("values", [[[1, 2]], [[0, 0, 0]], [[float('nan'), 1, 2]], [[True, 1, 2]], [['1', '2', '3']], []])
def test_invalid_embedding_shape_values_cannot_write(indexing_context, db_session, values):
    ids, _, _, backend, encoder, _, _ = indexing_context
    encoder.override = values
    with pytest.raises(IndexingError) as error:
        index(db_session, indexing_context)
    assert error.value.code == "invalid_embedding_output"
    assert not backend._client.collection_exists(collection_for(ids[1]))


def test_embedding_failure_is_safe_and_has_no_success_receipt(indexing_context, db_session):
    ids, storage, _, backend, encoder, _, _ = indexing_context
    encoder.failure_at = 1
    with pytest.raises(IndexingError) as error:
        index(db_session, indexing_context)
    assert error.value.code == "embedding_failed" and str(storage.root) not in str(error.value)
    assert not backend._client.collection_exists(collection_for(ids[1]))


def test_lost_upsert_ack_preserves_points_and_retry_is_idempotent(indexing_context, db_session, monkeypatch):
    ids, _, _, backend, encoder, _, _ = indexing_context
    original = backend._client.upsert
    def lost_ack(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("ack lost private sentinel")
    monkeypatch.setattr(backend._client, "upsert", lost_ack)
    with pytest.raises(IndexingError) as error:
        index(db_session, indexing_context)
    assert error.value.code == "index_write_failed"
    assert backend._client.count(collection_for(ids[1]), exact=True).count == 1
    calls = encoder.calls
    monkeypatch.setattr(backend._client, "upsert", original)
    assert index(db_session, indexing_context).verified
    assert encoder.calls == calls and backend._client.count(collection_for(ids[1]), exact=True).count == 1


@pytest.mark.parametrize("field", ["revision", "dimension"])
def test_collection_model_mismatch_never_recreates_or_overwrites(indexing_context, db_session, field):
    ids, _, _, backend, _, _, _ = indexing_context
    index(db_session, indexing_context)
    wrong = replace(indexing_context[5], **({"revision": "different-revision"} if field == "revision" else {"dimension": 2}))
    ids, storage, _, backend, encoder, _, _ = indexing_context
    with pytest.raises(IndexingError) as error:
        service.index_document_version(db_session, *ids, storage, index=backend, encoder=encoder, spec=wrong)
    assert error.value.code == "index_collection_mismatch"
    assert backend._client.count(collection_for(ids[1]), exact=True).count == 1


def test_foreign_origin_point_is_never_overwritten(indexing_context, db_session):
    ids, _, chunked, backend, _, _, _ = indexing_context
    index(db_session, indexing_context)
    identity = str(chunked.children[0].chunk_id)
    backend._client.set_payload(collection_for(ids[1]), payload={"metadata": {"document_version_id": str(uuid4())}}, points=[identity])
    before = backend._client.retrieve(collection_for(ids[1]), ids=[identity])[0].payload
    with pytest.raises(IndexingError) as error:
        index(db_session, indexing_context)
    assert error.value.code == "index_point_identity_conflict"
    assert backend._client.retrieve(collection_for(ids[1]), ids=[identity])[0].payload == before


@pytest.mark.parametrize("status", ["pending", "processing", "failed", "ready"])
def test_indexing_never_changes_overall_document_status(indexing_context, db_session, status):
    ids = indexing_context[0]
    db_session.execute(update(DocumentVersion).where(DocumentVersion.id == ids[3]).values(status=status))
    db_session.commit()
    assert index(db_session, indexing_context).verified
    assert db_session.scalar(select(DocumentVersion.status).where(DocumentVersion.id == ids[3])) == status


@pytest.mark.parametrize("url", ["http://example.com:6333", "http://user:secret@127.0.0.1:6333", "http://127.0.0.1:6333/path", "ftp://127.0.0.1:6333", "http://127.0.0.1:6333?key=secret"])
def test_untrusted_or_credential_bearing_urls_rejected(url):
    with pytest.raises(IndexingError) as error:
        IndexSettings(url=url)
    assert error.value.code == "invalid_index_settings" and "secret" not in str(error.value)


def test_internal_index_has_no_public_query_or_chat_coupling():
    import ast
    from project.api.app import create_app
    root = Path(__file__).resolve().parents[1]
    for name in ["project/core/index_models.py", "project/core/index_embeddings.py", "project/core/version_index.py", "project/api/document_indexing_service.py"]:
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        assert not any("rag_agent" in item or "vector_db_manager" in item or "document_manager" in item for item in imports)
    assert not any("index" in getattr(route, "path", "") for route in create_app().routes)
    assert not hasattr(VersionIndex, "search") and not hasattr(VersionIndex, "delete_collection")


# Construct actual generated PDF + SQL parse record + validated persisted chunks.
def new_inputs(session, storage, ids, *, other_kb=False, other_user=False, long=False, blank=False):
    from datetime import datetime, timezone
    from project.db.business_models import User, KnowledgeBase, Document
    from project.core.document_parser import DocumentVersionSource, parse_pdf_version
    from project.core.parse_artifacts import ParseArtifactStore
    owner, kb, document, _ = ids
    if other_user:
        user = User(email=f"index-{uuid4().hex}@example.com", password_hash="test-only")
        session.add(user)
        session.flush()
        owner = user.id
    if other_kb or other_user:
        record = KnowledgeBase(owner_id=owner, name="Index isolation")
        session.add(record)
        session.flush()
        kb = record.id
        doc = Document(knowledge_base_id=kb)
        session.add(doc)
        session.flush()
        document = doc.id
    item = DocumentVersion(document_id=document, original_filename="shared-name.pdf")
    session.add(item)
    session.commit()
    with pymupdf.open() as pdf:
        text = "Public long page " * 90 if long else "English public index page"
        lines = "\n".join(text[start:start+90] for start in range(0, len(text), 90))
        first = pdf.new_page()
        if not blank:
            first.insert_text((72,72), lines)
        pdf.new_page()
        third = pdf.new_page()
        if not blank:
            third.insert_text((72,72), "中文公开索引页", fontname="china-s")
        content = pdf.tobytes()
    storage.save(BytesIO(content), kb, document, item.id)
    source = DocumentVersionSource(kb, document, item.id, item.original_filename)
    parsed = parse_pdf_version(source, storage)
    from project.core.parse_artifacts import ParseArtifactStore
    artifacts = ParseArtifactStore(storage)
    attempt = uuid4()
    digest = artifacts.publish(artifacts.reserve(source, attempt), parsed)
    session.add(DocumentParseStage(document_version_id=item.id, attempt_id=attempt, status="succeeded",
                finished_at=datetime.now(timezone.utc), page_count=parsed.page_count, has_text=parsed.has_text, result_sha256=digest))
    session.commit()
    new_ids = owner, kb, document, item.id
    chunks = generate_document_version_chunks(session, *new_ids, storage)
    return new_ids, chunks


def test_partial_embedding_failure_retries_only_missing_points(indexing_context, db_session):
    ids, storage, _, backend, encoder, spec, _ = indexing_context
    new_ids, chunks = new_inputs(db_session, storage, ids, long=True)
    encoder.failure_at = 2
    with pytest.raises(IndexingError) as error:
        service.index_document_version(db_session, *new_ids, storage, index=backend, encoder=encoder, spec=spec)
    assert error.value.code == "embedding_failed"
    assert backend._client.count(collection_for(ids[1]), exact=True).count == 1
    encoder.failure_at = None
    receipt = service.index_document_version(db_session, *new_ids, storage, index=backend, encoder=encoder, spec=spec)
    assert receipt.verified and receipt.point_count == len(chunks.children) > 1
    assert backend._client.count(receipt.collection_name, exact=True).count == len(chunks.children)


def test_versions_and_kb_owners_are_isolated(indexing_context, db_session):
    ids, storage, first, backend, encoder, spec, _ = indexing_context
    original = index(db_session, indexing_context)
    before = backend._client.retrieve(original.collection_name, ids=[str(item.chunk_id) for item in first.children], with_vectors=True)
    second_ids, second = new_inputs(db_session, storage, ids)
    another_ids, another = new_inputs(db_session, storage, ids, other_user=True)
    for inputs in (second_ids, another_ids):
        service.index_document_version(db_session, *inputs, storage, index=backend, encoder=encoder, spec=spec)
    after = backend._client.retrieve(original.collection_name, ids=[str(item.chunk_id) for item in first.children], with_vectors=True)
    assert before == after
    assert backend._client.count(original.collection_name, exact=True).count == len(first.children) + len(second.children)
    assert backend._client.count(collection_for(another_ids[1]), exact=True).count == len(another.children)
    with pytest.raises(DocumentNotFound):
        service.index_document_version(db_session, ids[0], *another_ids[1:], storage, index=backend, encoder=encoder, spec=spec)


def test_source_change_during_embedding_prevents_first_write(indexing_context, db_session, monkeypatch):
    from datetime import datetime, timezone
    ids, _, _, backend, encoder, _, _ = indexing_context
    original = encoder.embed_documents
    def invalidate(texts):
        values = original(texts)
        db_session.execute(update(DocumentParseStage).where(DocumentParseStage.document_version_id == ids[3]).values(
            status="failed", page_count=None, has_text=None, result_sha256=None,
            finished_at=datetime.now(timezone.utc), error_code="artifact_unavailable"))
        db_session.commit()
        return values
    monkeypatch.setattr(encoder, "embed_documents", invalidate)
    with pytest.raises(IndexingError) as error:
        index(db_session, indexing_context)
    assert error.value.code == "source_parse_not_succeeded"
    assert not backend._client.collection_exists(collection_for(ids[1]))


def test_ack_without_actual_points_is_not_success(indexing_context, db_session, monkeypatch):
    from types import SimpleNamespace
    ids, _, _, backend, _, _, _ = indexing_context
    monkeypatch.setattr(backend._client, "upsert", lambda *a, **k: SimpleNamespace(status=models.UpdateStatus.COMPLETED))
    with pytest.raises(IndexingError) as error:
        index(db_session, indexing_context)
    assert error.value.code == "index_verification_failed"
    assert backend._client.count(collection_for(ids[1]), exact=True).count == 0


def test_missing_model_cache_never_downloads(monkeypatch):
    from project.core import index_embeddings as module
    module._cached_bundle.cache_clear()
    monkeypatch.setattr(module, "try_to_load_from_cache", lambda *a: None)
    with pytest.raises(IndexingError) as error:
        module.get_index_embeddings()
    assert error.value.code == "embedding_cache_missing"


def test_blank_pdf_has_no_indexable_text_or_model_backend_calls(indexing_context, db_session):
    ids, storage, _, backend, encoder, spec, _ = indexing_context
    inputs, chunks = new_inputs(db_session, storage, ids, blank=True)
    assert chunks.children == () and chunks.blank_page_numbers == (1, 2, 3)
    with pytest.raises(IndexingError) as error:
        service.index_document_version(db_session, *inputs, storage, index=backend, encoder=encoder, spec=spec)
    assert error.value.code == "no_indexable_text" and encoder.calls == 0
    assert not backend._client.collection_exists(collection_for(ids[1]))
