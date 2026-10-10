"""Knowledge-base-scoped Qdrant operations. No search or deletion API."""
from dataclasses import asdict
import math
from uuid import UUID

from qdrant_client import QdrantClient, models
from qdrant_client.http.exceptions import UnexpectedResponse

from project.core.index_models import IndexSettings, IndexingError

VECTOR_NAME = "dense"
SCHEMA_VERSION = 1


def collection_for(knowledge_base_id: UUID):
    if not isinstance(knowledge_base_id, UUID):
        raise IndexingError("invalid_index_identity")
    return "knowflow_kb_" + knowledge_base_id.hex


class VersionIndex:
    """Low-level trusted server capability; callers must authorize source first.

    Production uses the network service. An injected client is for isolated
    tests; neither the Demo manager nor its local directory is opened.
    """
    def __init__(self, settings=None, *, client=None):
        self.settings = IndexSettings.from_env() if settings is None else settings
        try:
            self._client = client if client is not None else QdrantClient(
                url=self.settings.url, timeout=self.settings.timeout_seconds,
                api_key=self.settings.api_key, prefer_grpc=False, trust_env=False,
            )
        except Exception:
            raise IndexingError("index_backend_unavailable") from None

    def close(self):
        try:
            self._client.close()
        except Exception:
            raise IndexingError("index_backend_unavailable") from None

    @staticmethod
    def _metadata(source, spec):
        return {"knowflow_index_schema": SCHEMA_VERSION, "knowledge_base_id": str(source.knowledge_base_id),
                "embedding": asdict(spec)}

    def _collection(self, source, spec, create=False):
        name = collection_for(source.knowledge_base_id)
        if not self._client.collection_exists(name):
            if not create:
                return None
            try:
                self._client.create_collection(name,
                    vectors_config={VECTOR_NAME: models.VectorParams(size=spec.dimension, distance=models.Distance.COSINE)},
                    metadata=self._metadata(source, spec))
            except UnexpectedResponse as error:
                # Another process may have created the same collection. Reuse
                # only a matching winner, never recreate or delete it.
                if error.status_code != 409:
                    raise
        info = self._client.get_collection(name)
        vectors = info.config.params.vectors
        params = vectors.get(VECTOR_NAME) if isinstance(vectors, dict) else None
        if (params is None or params.size != spec.dimension or params.distance != models.Distance.COSINE
                or info.config.metadata != self._metadata(source, spec)):
            raise IndexingError("index_collection_mismatch")
        return name

    def _valid_vector(self, record, spec):
        vector = record.vector.get(VECTOR_NAME) if isinstance(record.vector, dict) else None
        return (isinstance(vector, list) and len(vector) == spec.dimension
                and all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) for value in vector)
                and math.isclose(sum(value * value for value in vector), 1.0, rel_tol=1e-4, abs_tol=1e-4))

    def missing(self, source, spec, payloads):
        try:
            name = self._collection(source, spec)
            if name is None:
                return list(payloads)
            valid = set()
            ids = list(payloads)
            for start in range(0, len(ids), 64):
                records = self._client.retrieve(name, ids=ids[start:start + 64], with_payload=True, with_vectors=True)
                for record in records:
                    identity = str(record.id)
                    expected = payloads[identity]
                    metadata = (record.payload or {}).get("metadata", {})
                    origin_fields = ("knowledge_base_id", "document_id", "document_version_id", "chunk_id",
                                     "parse_attempt_id", "parse_sha256", "chunking_fingerprint")
                    if not isinstance(metadata, dict) or any(metadata.get(key) != expected["metadata"][key] for key in origin_fields):
                        raise IndexingError("index_point_identity_conflict")
                    if record.payload == expected and self._valid_vector(record, spec):
                        valid.add(identity)
            return [identity for identity in ids if identity not in valid]
        except IndexingError:
            raise
        except Exception:
            raise IndexingError("index_backend_unavailable") from None

    def write(self, source, spec, payloads, vectors):
        try:
            name = self._collection(source, spec, create=True)
            points = [models.PointStruct(id=identity, payload=payload, vector={VECTOR_NAME: vector})
                      for (identity, payload), vector in zip(payloads.items(), vectors, strict=True)]
            result = self._client.upsert(name, points=points, wait=True)
            if result.status != models.UpdateStatus.COMPLETED:
                raise IndexingError("index_write_unconfirmed")
            # Also compare the actual readback vectors, not just the ACK.
            returned = {str(point.id): point for point in self._client.retrieve(
                name, ids=list(payloads), with_payload=True, with_vectors=True)}
            for point in points:
                actual = returned.get(str(point.id))
                if (actual is None or actual.payload != point.payload or not self._valid_vector(actual, spec)
                        or not all(math.isclose(a, b, rel_tol=1e-5, abs_tol=1e-6)
                                   for a, b in zip(actual.vector[VECTOR_NAME], point.vector[VECTOR_NAME], strict=True))):
                    raise IndexingError("index_verification_failed")
        except IndexingError:
            raise
        except Exception:
            # A lost acknowledgement can hide a successful batch. Keep its
            # points for explicit idempotent retry; never compensate by deletion.
            raise IndexingError("index_write_failed") from None

    def verify(self, source, spec, payloads):
        if self.missing(source, spec, payloads):
            raise IndexingError("index_verification_failed")
        try:
            first = next(iter(payloads.values()))["metadata"]
            fields = ("knowledge_base_id", "document_id", "document_version_id", "parse_attempt_id",
                      "parse_sha256", "chunking_fingerprint", "embedding_fingerprint")
            scope = models.Filter(must=[models.FieldCondition(key="metadata." + key,
                                   match=models.MatchValue(value=first[key])) for key in fields])
            count = self._client.count(collection_for(source.knowledge_base_id), count_filter=scope, exact=True).count
            if count != len(payloads):
                raise IndexingError("index_verification_failed")
        except IndexingError:
            raise
        except Exception:
            raise IndexingError("index_backend_unavailable") from None
