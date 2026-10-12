"""Internal dense retrieval. owner_id MUST come from authenticated server context.

No public route, writes, readiness transition or automatic version selection.
Evidence is a checked snapshot; callers must authorize again for later requests.
"""
from dataclasses import dataclass, field

from project.api.document_indexing_service import _read, _payloads, _EMBEDDING_LOCK
from project.core.index_embeddings import get_index_embeddings
from project.core.index_models import EmbeddingSpec, IndexingError, normalized_vectors
from project.core.version_chunker import ParseBinding, VersionChunk
from project.core.version_index import VersionIndex


@dataclass(frozen=True, slots=True)
class RetrievalEvidence:
    child: VersionChunk = field(repr=False)
    parent: VersionChunk = field(repr=False)
    score: float


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    binding: ParseBinding
    chunking_fingerprint: str
    embedding: EmbeddingSpec
    evidence: tuple[RetrievalEvidence, ...] = field(repr=False)


def retrieve_document_version(session, owner_id, knowledge_base_id, document_id, version_id,
                              storage, query, *, limit=5, config=None, index=None,
                              encoder=None, spec=None):
    """Bounded top-k cosine matches, not an answer/confidence or relevance gate.

    Injected encoder/spec/backend are trusted server/test capabilities, never
    client options. SQL READ COMMITTED is required to observe concurrent changes.
    """
    chunked = _read(session, owner_id, knowledge_base_id, document_id, version_id, storage, config)
    if not isinstance(query, str) or not query.strip() or len(query) > 2000:
        raise IndexingError("invalid_retrieval_query")
    if type(limit) is not int or not 1 <= limit <= 20:
        raise IndexingError("invalid_retrieval_limit")
    if not chunked.children:
        raise IndexingError("no_indexable_text")
    if (encoder is None) != (spec is None):
        raise IndexingError("invalid_embedding_config")
    if encoder is None:
        encoder, spec = get_index_embeddings()
    owned = index is None
    index = VersionIndex() if owned else index
    try:
        payloads = _payloads(chunked, spec)
        # Reject missing/incompatible indexes before running query inference.
        index.verify(chunked.binding.source, spec, payloads)
        try:
            with _EMBEDDING_LOCK:
                vector = normalized_vectors([encoder.embed_query(query.strip())], 1, spec)[0]
        except IndexingError:
            raise
        except Exception:
            raise IndexingError("embedding_failed") from None
        hits = index.query_verified(chunked.binding.source, spec, payloads, vector, limit)
        if _read(session, owner_id, knowledge_base_id, document_id, version_id, storage, config) != chunked:
            raise IndexingError("source_changed")
        children = {str(item.chunk_id): item for item in chunked.children}
        parents = {item.chunk_id: item for item in chunked.parents}
        evidence = tuple(RetrievalEvidence(children[identity], parents[children[identity].parent_id], score)
                         for identity, score in hits)
        return RetrievalResult(chunked.binding, chunked.binding.fingerprint(chunked.config), spec, evidence)
    finally:
        if owned:
            index.close()
