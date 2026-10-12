from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from qdrant_client import models
from sqlalchemy import delete, select

from project.api import document_retrieval_service as service
from project.api.document_indexing_service import index_document_version, _payloads
from project.api.document_service import DocumentNotFound
from project.core.chunk_artifacts import ChunkArtifactStore
from project.core.index_models import IndexingError
from project.core.version_index import collection_for, VECTOR_NAME
from project.db.business_models import DocumentParseStage, DocumentVersion
from test_document_indexing import indexing_context, new_inputs, SyntheticEncoder
from test_document_chunking_service import chunk_service_context
from test_document_parsing_service import owned_version


class QueryEncoder(SyntheticEncoder):
    def embed_query(self, query):
        return self.embed_documents([query])[0]


@pytest.fixture
def retrieval_context(indexing_context, db_session):
    ids, storage, chunks, backend, _, spec, path = indexing_context
    encoder = QueryEncoder()
    index_document_version(db_session, *ids, storage, index=backend, encoder=encoder, spec=spec)
    return ids, storage, chunks, backend, encoder, spec, path


def retrieve(session, context, **kwargs):
    ids, storage, _, backend, encoder, spec, _ = context
    return service.retrieve_document_version(session, *ids, storage, "Public question",
        index=backend, encoder=encoder, spec=spec, **kwargs)


def test_trusted_evidence_repeat_and_no_writes(retrieval_context, db_session, monkeypatch):
    ids, storage, chunks, backend, _, spec, _ = retrieval_context
    before = {p: p.read_bytes() for p in storage.root.rglob("*") if p.is_file()}
    def forbid(*args, **kwargs):
        pytest.fail("Retrieval must not write")
    for method in ("commit", "flush"):
        monkeypatch.setattr(db_session, method, forbid)
    monkeypatch.setattr(backend._client, "upsert", forbid)
    result = retrieve(db_session, retrieval_context)
    assert result == retrieve(db_session, retrieval_context)
    assert result.binding == chunks.binding and result.embedding == spec
    assert result.chunking_fingerprint == chunks.binding.fingerprint(chunks.config)
    hit, = result.evidence
    assert hit.child == chunks.children[0] and hit.parent == chunks.parents[0]
    assert hit.child.text == "Public version one" and hit.child.page_number == 1
    assert hit.child.parent_id == hit.parent.chunk_id
    assert "Public version one" not in repr(result) + repr(hit)
    assert before == {p: p.read_bytes() for p in storage.root.rglob("*") if p.is_file()}
    with db_session.no_autoflush:
        assert db_session.scalar(select(DocumentVersion.status).where(DocumentVersion.id == ids[3])) == "pending"


@pytest.mark.parametrize("position", range(4))
def test_wrong_identity_before_io(retrieval_context, db_session, monkeypatch, position):
    ids, storage, _, backend, _, _, _ = retrieval_context
    wrong = list(ids)
    wrong[position] = uuid4()
    monkeypatch.setattr(service, "get_index_embeddings", lambda: pytest.fail("Unauthorized model"))
    monkeypatch.setattr(backend, "verify", lambda *args: pytest.fail("Unauthorized Qdrant"))
    with pytest.raises(DocumentNotFound):
        service.retrieve_document_version(db_session, *wrong, storage, "q", index=backend)


@pytest.mark.parametrize("kind", ["parse_missing", "parse_corrupt", "chunks_missing", "chunks_corrupt", "parse_not_succeeded"])
def test_untrusted_source_before_query(retrieval_context, db_session, monkeypatch, kind):
    ids, storage, chunks, backend, _, _, path = retrieval_context
    if kind == "parse_not_succeeded":
        db_session.execute(delete(DocumentParseStage).where(DocumentParseStage.document_version_id == ids[3]))
        db_session.commit()
    else:
        path = path if kind.startswith("parse") else ChunkArtifactStore(storage).path_for(chunks.binding, chunks.config)
        if kind.endswith("missing"):
            path.unlink()
        else:
            path.write_bytes(b"{broken")
    monkeypatch.setattr(backend, "verify", lambda *args: pytest.fail("Bad source reached Qdrant"))
    with pytest.raises(IndexingError, match="source_"):
        retrieve(db_session, retrieval_context)


@pytest.mark.parametrize("query,limit", [("", 5), ("  ", 5), (None, 5), ("x" * 2001, 5), ("q", 0), ("q", 21), ("q", True)])
def test_bounded_inputs(retrieval_context, db_session, query, limit):
    ids, storage, _, backend, encoder, spec, _ = retrieval_context
    calls = encoder.calls
    with pytest.raises(IndexingError, match="invalid_retrieval"):
        service.retrieve_document_version(db_session, *ids, storage, query, limit=limit,
                                         index=backend, encoder=encoder, spec=spec)
    assert encoder.calls == calls


@pytest.mark.parametrize("mutation", ["missing", "extra", "text", "identity", "vector"])
def test_entire_index_checked_before_query(retrieval_context, db_session, monkeypatch, mutation):
    ids, _, chunks, backend, _, spec, _ = retrieval_context
    name = collection_for(ids[1])
    identity = str(chunks.children[0].chunk_id)
    if mutation == "missing":
        backend._client.delete(name, models.PointIdsList(points=[identity]))
    elif mutation == "extra":
        payload = _payloads(chunks, spec)[identity]
        backend._client.upsert(name, [models.PointStruct(id=str(uuid4()), payload=payload, vector={VECTOR_NAME: [1., 0., 0.]})])
    elif mutation == "vector":
        backend._client.update_vectors(name, [models.PointVectors(id=identity, vector={VECTOR_NAME: [0., 0., 0.]})])
    else:
        payload = {"page_content": "forged"} if mutation == "text" else {"metadata": {"document_version_id": str(uuid4())}}
        backend._client.set_payload(name, payload, points=[identity])
    monkeypatch.setattr(backend._client, "query_points", lambda *a, **k: pytest.fail("Incomplete index queried"))
    with pytest.raises(IndexingError):
        retrieve(db_session, retrieval_context)


@pytest.mark.parametrize("kind", ["id", "payload", "duplicate", "empty", "score"])
def test_untrusted_backend_hits_fail_closed(retrieval_context, db_session, monkeypatch, kind):
    _, _, _, backend, _, _, _ = retrieval_context
    original = backend._client.query_points
    def altered(*args, **kwargs):
        result = original(*args, **kwargs)
        point = result.points[0]
        if kind == "id":
            point.id = str(uuid4())
        elif kind == "payload":
            point.payload["page_content"] = "private forged sentinel"
        elif kind == "score":
            point.score = float("nan")
        elif kind == "duplicate":
            result.points.append(point)
        else:
            result.points.clear()
        return result
    monkeypatch.setattr(backend._client, "query_points", altered)
    with pytest.raises(IndexingError, match="retrieval_verification_failed"):
        retrieve(db_session, retrieval_context)


def test_source_change_during_query_rejected(retrieval_context, db_session, monkeypatch):
    _, _, _, backend, _, _, parse_path = retrieval_context
    original = backend._client.query_points
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        parse_path.write_bytes(b"changed")
        return result
    monkeypatch.setattr(backend._client, "query_points", changed)
    with pytest.raises(IndexingError, match="source_"):
        retrieve(db_session, retrieval_context)


def test_index_change_during_query_rejected(retrieval_context, db_session, monkeypatch):
    ids, _, chunks, backend, _, _, _ = retrieval_context
    original = backend._client.query_points
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        backend._client.delete(collection_for(ids[1]), models.PointIdsList(points=[str(chunks.children[0].chunk_id)]))
        return result
    monkeypatch.setattr(backend._client, "query_points", changed)
    with pytest.raises(IndexingError, match="index_verification_failed"):
        retrieve(db_session, retrieval_context)


@pytest.mark.parametrize("kind", ["revision", "dimension", "encoder_failure", "bad_vector", "backend_failure"])
def test_model_and_backend_errors_safe(retrieval_context, db_session, monkeypatch, kind):
    ids, storage, _, backend, encoder, spec, _ = retrieval_context
    if kind in ("revision", "dimension"):
        spec = replace(spec, **({"revision": "wrong"} if kind == "revision" else {"dimension": 2}))
    elif kind == "encoder_failure":
        encoder.failure_at = encoder.calls + 1
    elif kind == "bad_vector":
        encoder.override = [[0., 0., 0.]]
    else:
        def broken(*args, **kwargs):
            raise RuntimeError("private sentinel password")
        monkeypatch.setattr(backend._client, "query_points", broken)
    with pytest.raises(IndexingError) as error:
        service.retrieve_document_version(db_session, *ids, storage, "query", index=backend, encoder=encoder, spec=spec)
    assert "sentinel" not in str(error.value) and "password" not in str(error.value)


def test_version_and_owner_isolation_physical_pages(retrieval_context, db_session):
    ids, storage, _, backend, encoder, spec, _ = retrieval_context
    second_ids, second = new_inputs(db_session, storage, ids)
    other_ids, _ = new_inputs(db_session, storage, ids, other_user=True)
    for inputs in (second_ids, other_ids):
        index_document_version(db_session, *inputs, storage, index=backend, encoder=encoder, spec=spec)
    result = service.retrieve_document_version(db_session, *second_ids, storage, "question", index=backend, encoder=encoder, spec=spec)
    assert {e.child.page_number for e in result.evidence} == {1, 3}
    assert {e.child.chunk_id for e in result.evidence} == {c.chunk_id for c in second.children}
    assert all(e.child.document_version_id == second_ids[3] for e in result.evidence)
    assert all(e.parent.page_number == e.child.page_number for e in result.evidence)
    with pytest.raises(DocumentNotFound):
        service.retrieve_document_version(db_session, ids[0], *other_ids[1:], storage, "question", index=backend, encoder=encoder, spec=spec)


def test_no_public_api_or_rag_coupling():
    import ast
    from project.api.app import create_app
    paths = set(create_app().openapi()["paths"])
    assert not any("retriev" in path or "search" in path for path in paths)
    tree = ast.parse(Path(service.__file__).read_text())
    imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert not any("rag_agent" in name or "vector_db_manager" in name for name in imports)


def test_partial_index_refuses_until_explicit_retry(retrieval_context, db_session):
    ids, storage, _, backend, encoder, spec, _ = retrieval_context
    inputs, chunks = new_inputs(db_session, storage, ids, long=True)
    encoder.failure_at = encoder.calls + 2
    with pytest.raises(IndexingError, match="embedding_failed"):
        index_document_version(db_session, *inputs, storage, index=backend, encoder=encoder, spec=spec)
    with pytest.raises(IndexingError, match="index_verification_failed"):
        service.retrieve_document_version(db_session, *inputs, storage, "question", index=backend, encoder=encoder, spec=spec)
    encoder.failure_at = None
    index_document_version(db_session, *inputs, storage, index=backend, encoder=encoder, spec=spec)
    result = service.retrieve_document_version(db_session, *inputs, storage, "question", index=backend, encoder=encoder, spec=spec, limit=1)
    assert len(result.evidence) == 1
    assert result.evidence[0].child.document_version_id == inputs[3]


def test_blank_version_has_no_retrievable_text(retrieval_context, db_session):
    ids, storage, _, backend, encoder, spec, _ = retrieval_context
    inputs, _ = new_inputs(db_session, storage, ids, blank=True)
    with pytest.raises(IndexingError, match="no_indexable_text"):
        service.retrieve_document_version(db_session, *inputs, storage, "question", index=backend, encoder=encoder, spec=spec)


def test_current_chunk_config_cannot_reuse_old_index(retrieval_context, db_session):
    from project.api.document_chunking_service import generate_document_version_chunks
    ids, storage, chunks, backend, encoder, spec, _ = retrieval_context
    config = replace(chunks.config, child_chunk_overlap=0)
    assert config != chunks.config
    generate_document_version_chunks(db_session, *ids, storage, config)
    with pytest.raises(IndexingError, match="index_verification_failed"):
        service.retrieve_document_version(db_session, *ids, storage, "question", config=config,
                                         index=backend, encoder=encoder, spec=spec)
