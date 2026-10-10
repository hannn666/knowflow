"""Small immutable indexing contracts; no document lifecycle state."""
from dataclasses import asdict, dataclass, field
from importlib.metadata import version
import hashlib
import math
import os
from numbers import Real
from urllib.parse import urlsplit
from uuid import UUID

from project.core.document_parser import DocumentVersionSource
from project.core.version_chunker import canonical_json


class IndexingError(Exception):
    """Safe machine codes only. Failure never supplies a success receipt."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class EmbeddingSpec:
    model: str
    revision: str
    dimension: int
    normalized: bool = True
    implementation: str = "sentence-transformers"
    implementation_version: str = version("sentence-transformers")
    wrapper_version: str = version("langchain-huggingface")

    def __post_init__(self):
        if (not isinstance(self.model, str) or not self.model or len(self.model) > 255
                or not isinstance(self.revision, str) or not self.revision or len(self.revision) > 128
                or type(self.dimension) is not int or not 1 <= self.dimension <= 4096
                or self.normalized is not True):
            raise IndexingError("invalid_embedding_config")

    @property
    def fingerprint(self):
        return hashlib.sha256(canonical_json(asdict(self))).hexdigest()


@dataclass(frozen=True, slots=True)
class IndexSettings:
    url: str = "http://127.0.0.1:6333"
    timeout_seconds: int = 10
    batch_size: int = 8
    api_key: str | None = field(default=None, repr=False)

    def __post_init__(self):
        try:
            parsed = urlsplit(self.url)
            valid = (parsed.scheme in {"http", "https"} and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                     and parsed.username is None and parsed.password is None and not parsed.query and not parsed.fragment
                     and parsed.path in {"", "/"} and parsed.port is not None and 1 <= parsed.port <= 65535)
        except (ValueError, TypeError):
            valid = False
        if (not valid or type(self.batch_size) is not int or not 1 <= self.batch_size <= 64
                or type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 120
                or (self.api_key is not None and (not isinstance(self.api_key, str) or not self.api_key.strip()))):
            raise IndexingError("invalid_index_settings")

    @classmethod
    def from_env(cls):
        return cls(url=os.getenv("KNOWFLOW_QDRANT_URL", cls.__dataclass_fields__["url"].default),
                   api_key=os.getenv("KNOWFLOW_QDRANT_API_KEY") or None)


@dataclass(frozen=True, slots=True)
class IndexReceipt:
    source: DocumentVersionSource
    collection_name: str
    parse_attempt_id: UUID
    parse_sha256: str
    chunking_fingerprint: str
    embedding: EmbeddingSpec
    point_count: int
    parent_count: int
    verified: bool = True


def normalized_vectors(raw, expected_count, spec):
    try:
        if len(raw) != expected_count:
            raise ValueError
        output = []
        for vector in raw:
            if len(vector) != spec.dimension or any(not isinstance(value, Real) or isinstance(value, bool) for value in vector):
                raise ValueError
            values = [float(value) for value in vector]
            if not all(math.isfinite(value) for value in values):
                raise ValueError
            norm = math.sqrt(sum(value * value for value in values))
            if not math.isfinite(norm) or norm <= 0:
                raise ValueError
            output.append([value / norm for value in values])
        return output
    except (TypeError, ValueError, OverflowError):
        raise IndexingError("invalid_embedding_output") from None
