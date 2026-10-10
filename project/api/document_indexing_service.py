"""Protected internal indexing; owner_id comes from authentication/server context.

No HTTP route, query API, DB commit/flush or document readiness change. A receipt
is a verified snapshot, not a permanent lifecycle state or retrieval permission.
"""
from dataclasses import asdict
import json
from threading import Lock

from project.api.document_chunking_service import read_document_version_chunks
from project.core.index_embeddings import get_index_embeddings
from project.core.index_models import IndexingError, IndexReceipt, normalized_vectors
from project.core.version_chunker import ALGORITHM, ChunkingError, canonical_json
from project.core.version_index import SCHEMA_VERSION, VersionIndex, collection_for

_EMBEDDING_LOCK = Lock()


def _read(session, owner_id, kb_id, document_id, version_id, storage, config):
    try:
        return read_document_version_chunks(session, owner_id, kb_id, document_id, version_id, storage, config)
    except ChunkingError as error:
        raise IndexingError("source_" + error.code) from None


def _payloads(chunked, spec):
    fingerprint = chunked.binding.fingerprint(chunked.config)
    result = {}
    for chunk in chunked.children:
        metadata = {"index_schema": SCHEMA_VERSION,
                    "knowledge_base_id": str(chunk.knowledge_base_id), "document_id": str(chunk.document_id),
                    "document_version_id": str(chunk.document_version_id), "original_filename": chunk.original_filename,
                    "chunk_id": str(chunk.chunk_id), "parent_id": str(chunk.parent_id),
                    "page_number": chunk.page_number, "chunk_order": chunk.order,
                    "parse_attempt_id": str(chunked.binding.attempt_id), "parse_sha256": chunked.binding.sha256,
                    "chunking_fingerprint": fingerprint, "chunking_algorithm": ALGORITHM,
                    "chunking_config": asdict(chunked.config), "embedding_spec": asdict(spec),
                    "embedding_fingerprint": spec.fingerprint}
        result[str(chunk.chunk_id)] = {"page_content": chunk.text, "metadata": metadata}
    return json.loads(canonical_json(result))


def index_document_version(session, owner_id, knowledge_base_id, document_id, version_id,
                           storage, *, config=None, index=None, encoder=None, spec=None):
    # Permission and every source artifact are checked before backend/model IO.
    chunked = _read(session, owner_id, knowledge_base_id, document_id, version_id, storage, config)
    if not chunked.children:
        raise IndexingError("no_indexable_text")
    if (encoder is None) != (spec is None):
        raise IndexingError("invalid_embedding_config")
    if encoder is None:
        encoder, spec = get_index_embeddings()
    owned = index is None
    index = VersionIndex() if owned else index
    source = chunked.binding.source
    try:
        payloads = _payloads(chunked, spec)
        pending = index.missing(source, spec, payloads)
        for start in range(0, len(pending), index.settings.batch_size):
            ids = pending[start:start + index.settings.batch_size]
            texts = [payloads[identity]["page_content"] for identity in ids]
            try:
                # Bound batch memory and serialize the shared cached encoder in
                # this process. Qdrant itself handles independent API clients.
                with _EMBEDDING_LOCK:
                    vectors = normalized_vectors(encoder.embed_documents(texts), len(ids), spec)
            except IndexingError:
                raise
            except Exception:
                raise IndexingError("embedding_failed") from None
            if start == 0 and _read(session, owner_id, knowledge_base_id, document_id, version_id, storage, config) != chunked:
                raise IndexingError("source_changed")
            index.write(source, spec, {identity: payloads[identity] for identity in ids}, vectors)
        index.verify(source, spec, payloads)
        if _read(session, owner_id, knowledge_base_id, document_id, version_id, storage, config) != chunked:
            raise IndexingError("source_changed")
        return IndexReceipt(source, collection_for(source.knowledge_base_id), chunked.binding.attempt_id,
                            chunked.binding.sha256, chunked.binding.fingerprint(chunked.config), spec,
                            len(chunked.children), len(chunked.parents))
    finally:
        if owned:
            index.close()
