"""Opt-in real HTTP Qdrant service tests; synthetic encoder is labelled."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest
from qdrant_client import QdrantClient, models

from project.api.document_chunking_service import generate_document_version_chunks
from project.api.document_indexing_service import index_document_version, _payloads
from project.core.index_embeddings import get_index_embeddings
from project.core.index_models import EmbeddingSpec, IndexSettings, IndexingError, normalized_vectors
from project.core.version_index import VersionIndex, VECTOR_NAME, collection_for
from project.db.business_models import DocumentVersion
from test_document_indexing import SyntheticEncoder, new_inputs
from test_document_chunking_service import chunk_service_context
from test_document_parsing_service import owned_version


@pytest.fixture
def live_index(chunk_service_context, db_session):
    if os.getenv("KNOWFLOW_TEST_QDRANT") != "1":
        pytest.skip("Opt-in real Qdrant service required")
    ids, storage, _ = chunk_service_context
    generate_document_version_chunks(db_session, *ids, storage)
    backend = VersionIndex(IndexSettings(batch_size=1))
    baseline = {item.name for item in backend._client.get_collections().collections}
    allocated = {}
    def own(kb_id):
        name = collection_for(kb_id)
        if name not in allocated:
            assert name not in baseline, "Generated test KB must not reuse an existing collection"
            allocated[name] = str(kb_id)
        return name
    try:
        own(ids[1])
        yield ids, storage, backend, own
    finally:
        # Test resources only: exact generated UUIDs, absent at test start, and
        # owner/schema verified. Production backend has no deletion method.
        for name, kb_id in allocated.items():
            if backend._client.collection_exists(name):
                metadata = backend._client.get_collection(name).config.metadata
                assert metadata["knowledge_base_id"] == kb_id and metadata["knowflow_index_schema"] == 1
                backend._client.delete_collection(name)
        assert {item.name for item in backend._client.get_collections().collections} == baseline
        backend.close()


def test_real_service_pages_ownership_versions_and_independent_client(live_index, db_session):
    ids, storage, backend, own = live_index
    spec = EmbeddingSpec("test-only-synthetic", "synthetic-v1", 3)
    encoder = SyntheticEncoder()
    first = generate_document_version_chunks(db_session, *ids, storage)
    initial = index_document_version(db_session, *ids, storage, index=backend, encoder=encoder, spec=spec)
    second_ids, second = new_inputs(db_session, storage, ids)
    other_ids, other = new_inputs(db_session, storage, ids, other_user=True)
    own(other_ids[1])
    for inputs in (second_ids, other_ids):
        index_document_version(db_session, *inputs, storage, index=backend, encoder=encoder, spec=spec)
    independent = QdrantClient(url=backend.settings.url, trust_env=False)
    try:
        assert independent.count(initial.collection_name, exact=True).count == len(first.children) + len(second.children)
        assert independent.count(collection_for(other_ids[1]), exact=True).count == len(other.children)
        points = independent.retrieve(initial.collection_name, ids=[str(item.chunk_id) for item in second.children], with_vectors=True)
        assert {point.payload["metadata"]["page_number"] for point in points} == {1, 3}
        parents = {str(item.chunk_id) for item in second.parents}
        assert all(point.payload["metadata"]["parent_id"] in parents for point in points)
        assert all(point.payload["metadata"]["document_version_id"] == str(second_ids[3]) for point in points)
    finally:
        independent.close()
    calls = encoder.calls
    assert index_document_version(db_session, *ids, storage, index=backend, encoder=encoder, spec=spec) == initial
    assert encoder.calls == calls
    assert db_session.get(DocumentVersion, ids[3]).status == "pending"


def test_real_service_partial_failure_and_explicit_retry(live_index, db_session):
    ids, storage, backend, own = live_index
    inputs, chunks = new_inputs(db_session, storage, ids, long=True)
    encoder = SyntheticEncoder()
    encoder.failure_at = 2
    spec = EmbeddingSpec("test-only-synthetic", "synthetic-v1", 3)
    with pytest.raises(IndexingError) as error:
        index_document_version(db_session, *inputs, storage, index=backend, encoder=encoder, spec=spec)
    assert error.value.code == "embedding_failed"
    assert backend._client.count(collection_for(ids[1]), exact=True).count == 1
    encoder.failure_at = None
    result = index_document_version(db_session, *inputs, storage, index=backend, encoder=encoder, spec=spec)
    assert result.point_count == len(chunks.children)
    assert backend._client.count(result.collection_name, exact=True).count == result.point_count


def test_two_network_clients_concurrently_index_same_version(live_index, db_session):
    ids, storage, backend, _ = live_index
    chunked = generate_document_version_chunks(db_session, *ids, storage)
    spec = EmbeddingSpec("test-only-synthetic", "synthetic-v1", 3)
    payloads = _payloads(chunked, spec)
    vectors = normalized_vectors(SyntheticEncoder().embed_documents([value["page_content"] for value in payloads.values()]), len(payloads), spec)
    def write_on_own_client(_):
        actor = VersionIndex(backend.settings)
        try:
            actor.write(chunked.binding.source, spec, payloads, vectors)
            actor.verify(chunked.binding.source, spec, payloads)
            return True
        finally:
            actor.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(write_on_own_client, range(2))) == [True, True]
    assert backend._client.count(collection_for(ids[1]), exact=True).count == len(payloads)


def test_separate_process_can_read_same_service(live_index, db_session):
    ids, storage, backend, _ = live_index
    spec = EmbeddingSpec("test-only-synthetic", "synthetic-v1", 3)
    result = index_document_version(db_session, *ids, storage, index=backend, encoder=SyntheticEncoder(), spec=spec)
    script = "from qdrant_client import QdrantClient; import sys; c=QdrantClient(url=sys.argv[1],trust_env=False); assert c.count(sys.argv[2],exact=True).count==int(sys.argv[3]); c.close(); print('PASS: independent process service read')"
    child = subprocess.run([sys.executable, "-c", script, backend.settings.url, result.collection_name, str(result.point_count)], capture_output=True, timeout=30)
    assert child.returncode == 0, child.stderr.decode(errors="replace")
    assert b"PASS: independent process" in child.stdout


def test_real_cached_model_and_server_vector_readback(live_index, db_session):
    if os.getenv("KNOWFLOW_TEST_REAL_EMBEDDING") != "1":
        pytest.skip("Explicit cached real model inference required")
    ids, storage, backend, _ = live_index
    encoder, spec = get_index_embeddings()
    assert spec.model == "Qwen/Qwen3-Embedding-0.6B" and spec.dimension == 1024
    inputs, chunks = new_inputs(db_session, storage, ids)
    result = index_document_version(db_session, *inputs, storage, index=backend, encoder=encoder, spec=spec)
    assert result.verified and result.point_count == len(chunks.children)
    points = backend._client.retrieve(result.collection_name, ids=[str(item.chunk_id) for item in chunks.children], with_vectors=True)
    assert all(len(point.vector[VECTOR_NAME]) == 1024 for point in points)
    assert all(point.payload["metadata"]["embedding_spec"]["revision"] == spec.revision for point in points)
    expected = normalized_vectors(encoder.embed_documents([item.text for item in chunks.children]), len(chunks.children), spec)
    import math
    by_id = {str(point.id): point for point in points}
    for item, vector in zip(chunks.children, expected, strict=True):
        assert all(math.isclose(a,b,rel_tol=1e-5,abs_tol=1e-6) for a,b in zip(vector, by_id[str(item.chunk_id)].vector[VECTOR_NAME], strict=True))
    assert db_session.get(DocumentVersion, inputs[3]).status == "pending"
