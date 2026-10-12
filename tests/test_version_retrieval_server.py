"""Opt-in real Qdrant network queries; model inference is separately opted in."""
from copy import deepcopy
import os
from uuid import uuid4

import pytest
from qdrant_client import models

from project.api.document_indexing_service import index_document_version, _payloads
from project.api.document_retrieval_service import retrieve_document_version
from project.core.index_embeddings import get_index_embeddings
from project.core.index_models import EmbeddingSpec
from project.core.version_index import VersionIndex, VECTOR_NAME, collection_for
from test_version_index_server import live_index
from test_document_indexing import new_inputs
from test_document_retrieval import QueryEncoder
from test_document_chunking_service import chunk_service_context
from test_document_parsing_service import owned_version


def test_real_strict_filter_excludes_each_wrong_binding(live_index, db_session):
    ids, storage, backend, own = live_index
    spec = EmbeddingSpec("test-only-synthetic", "synthetic-v1", 3)
    encoder = QueryEncoder()
    encoder.embed_query = lambda _: [1., 0., 0.]
    inputs, chunks = new_inputs(db_session, storage, ids)
    index_document_version(db_session, *inputs, storage, index=backend, encoder=encoder, spec=spec)
    name = collection_for(ids[1])
    payload = next(iter(_payloads(chunks, spec).values()))
    decoys = []
    fields = ("knowledge_base_id", "document_id", "document_version_id", "parse_attempt_id",
              "parse_sha256", "chunking_fingerprint", "embedding_fingerprint")
    for field in fields:
        wrong = deepcopy(payload)
        wrong["metadata"][field] = str(uuid4())
        decoys.append(models.PointStruct(id=str(uuid4()), payload=wrong, vector={VECTOR_NAME: [1., 0., 0.]}))
    backend._client.upsert(name, decoys, wait=True)
    unfiltered = backend._client.query_points(name, query=[1., 0., 0.], using=VECTOR_NAME, limit=1).points
    assert str(unfiltered[0].id) in {str(p.id) for p in decoys}
    independent = VersionIndex(backend.settings)
    try:
        result = retrieve_document_version(db_session, *inputs, storage, "question", index=independent,
                                           encoder=encoder, spec=spec, limit=20)
    finally:
        independent.close()
    assert {hit.child.chunk_id for hit in result.evidence} == {c.chunk_id for c in chunks.children}
    assert {hit.child.page_number for hit in result.evidence} == {1, 3}
    assert all(hit.child.parent_id == hit.parent.chunk_id for hit in result.evidence)
    assert backend._client.count(name, exact=True).count == len(chunks.children) + len(decoys)


def test_real_cached_model_retrieval(live_index, db_session):
    if os.getenv("KNOWFLOW_TEST_REAL_EMBEDDING") != "1":
        pytest.skip("Explicit cached real model inference required")
    ids, storage, backend, own = live_index
    encoder, spec = get_index_embeddings()
    index_document_version(db_session, *ids, storage, index=backend, encoder=encoder, spec=spec)
    result = retrieve_document_version(db_session, *ids, storage, "Public version one",
                                       index=backend, encoder=encoder, spec=spec)
    hit, = result.evidence
    assert hit.child.text == "Public version one" and hit.child.page_number == 1
    assert hit.score > 0.99
    assert result.embedding.dimension == 1024
