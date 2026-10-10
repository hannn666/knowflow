"""Page-local adaptation of the upstream parent/child chunker; no indexing."""
from dataclasses import asdict, dataclass, field
import hashlib
from importlib.metadata import version
import json
import re
from uuid import NAMESPACE_URL, UUID, uuid5

from project import config as upstream_config
from project.document_chunker import DocumentChunker
from project.core.document_parser import DocumentVersionSource, ParsedPdf
from project.core.parse_artifacts import ParseArtifactStore

ALGORITHM = "page-parent-child-v1"
MAX_CHUNKS = 50_000
MAX_CHUNK_TEXT_CHARACTERS = 8_000_000


class ChunkingError(Exception):
    """Fixed machine codes only; no private contents, credentials or paths."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def canonical_json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


@dataclass(frozen=True, slots=True)
class ChunkingConfig:
    min_parent_size: int = upstream_config.MIN_PARENT_SIZE
    max_parent_size: int = upstream_config.MAX_PARENT_SIZE
    child_chunk_size: int = upstream_config.CHILD_CHUNK_SIZE
    child_chunk_overlap: int = upstream_config.CHILD_CHUNK_OVERLAP
    headers: tuple[tuple[str, str], ...] = tuple(upstream_config.HEADERS_TO_SPLIT_ON)
    splitter_version: str = field(default=version("langchain-text-splitters"), init=False)

    def __post_init__(self):
        values = (self.min_parent_size, self.max_parent_size, self.child_chunk_size, self.child_chunk_overlap)
        if (any(type(value) is not int for value in values)
                or not 64 <= self.min_parent_size <= self.max_parent_size <= 16_000
                or not 64 <= self.child_chunk_size <= self.max_parent_size
                or not 0 <= self.child_chunk_overlap <= self.child_chunk_size // 2
                or self.child_chunk_overlap >= self.max_parent_size
                or type(self.headers) is not tuple
                or any(type(item) is not tuple or len(item) != 2 or not all(isinstance(value, str) and value for value in item) for item in self.headers)):
            raise ChunkingError("invalid_chunking_config")


@dataclass(frozen=True, slots=True)
class ParseBinding:
    source: DocumentVersionSource
    attempt_id: UUID
    sha256: str

    def __post_init__(self):
        if not isinstance(self.attempt_id, UUID) or not isinstance(self.sha256, str) or not re.fullmatch("[0-9a-f]{64}", self.sha256):
            raise ChunkingError("parse_artifact_invalid")

    def fingerprint(self, config: ChunkingConfig) -> str:
        return hashlib.sha256(canonical_json({"binding": asdict(self), "config": asdict(config), "algorithm": ALGORITHM})).hexdigest()


@dataclass(frozen=True, slots=True)
class VersionChunk(DocumentVersionSource):
    chunk_id: UUID
    page_number: int
    order: int
    text: str = field(repr=False)
    parent_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ChunkedVersion:
    binding: ParseBinding
    config: ChunkingConfig
    page_count: int
    blank_page_numbers: tuple[int, ...]
    parents: tuple[VersionChunk, ...] = field(repr=False)
    children: tuple[VersionChunk, ...] = field(repr=False)


def chunk_id(fingerprint: str, kind: str, page_number: int, order: int, text: str) -> UUID:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return uuid5(NAMESPACE_URL, f"knowflow:{fingerprint}:{kind}:{page_number}:{order}:{digest}")


def validate_chunks(result: ChunkedVersion, binding: ParseBinding, config: ChunkingConfig) -> None:
    if result.binding != binding or result.config != config or type(result.page_count) is not int or not 1 <= result.page_count <= 1000:
        raise ChunkingError("chunk_artifact_invalid")
    blanks = result.blank_page_numbers
    if (any(type(number) is not int or not 1 <= number <= result.page_count for number in blanks)
            or tuple(sorted(set(blanks))) != blanks):
        raise ChunkingError("chunk_artifact_invalid")
    fingerprint = binding.fingerprint(config)
    parent_map = {}
    count = len(result.parents) + len(result.children)
    if count > MAX_CHUNKS or sum(len(item.text) for item in (*result.parents, *result.children)) > MAX_CHUNK_TEXT_CHARACTERS:
        raise ChunkingError("chunk_resource_limit")
    for kind, chunks in (("parent", result.parents), ("child", result.children)):
        previous_page = 0
        for order, item in enumerate(chunks, 1):
            source = DocumentVersionSource(item.knowledge_base_id, item.document_id, item.document_version_id, item.original_filename)
            if (source != binding.source or type(item.order) is not int or item.order != order
                    or type(item.page_number) is not int or not previous_page <= item.page_number <= result.page_count
                    or item.page_number < 1 or item.page_number in blanks
                    or not isinstance(item.text, str) or not item.text.strip()
                    or item.chunk_id != chunk_id(fingerprint, kind, item.page_number, order, item.text)):
                raise ChunkingError("chunk_artifact_invalid")
            previous_page = item.page_number
            if kind == "parent":
                if item.parent_id is not None or len(item.text) > config.max_parent_size:
                    raise ChunkingError("chunk_artifact_invalid")
                parent_map[item.chunk_id] = item
            else:
                parent = parent_map.get(item.parent_id)
                if parent is None or parent.page_number != item.page_number or item.text not in parent.text or len(item.text) > config.child_chunk_size:
                    raise ChunkingError("chunk_artifact_invalid")
    if set(parent_map) != {item.parent_id for item in result.children}:
        raise ChunkingError("chunk_artifact_invalid")
    if {item.page_number for item in result.parents} | set(blanks) != set(range(1, result.page_count + 1)):
        raise ChunkingError("chunk_artifact_invalid")


def chunk_parsed_version(parsed: ParsedPdf, binding: ParseBinding, config: ChunkingConfig) -> ChunkedVersion:
    # The caller must first validate the persisted parse artifact against SQL.
    ParseArtifactStore.validate(parsed, binding.source)
    splitter = DocumentChunker(min_parent_size=config.min_parent_size, max_parent_size=config.max_parent_size,
                               child_chunk_size=config.child_chunk_size, child_chunk_overlap=config.child_chunk_overlap,
                               headers=config.headers)
    fingerprint = binding.fingerprint(config)
    parents, children, blanks = [], [], []
    source = binding.source
    for page in parsed.pages:
        if not page.page_text.strip():
            blanks.append(page.page_number)
            continue
        raw_parents, raw_children = splitter.create_chunks_text(
            page.page_text, source_id=f"page_{page.page_number}", source_name=source.original_filename)
        parent_ids = {}
        for local_id, raw in raw_parents:
            order = len(parents) + 1
            identity = chunk_id(fingerprint, "parent", page.page_number, order, raw.page_content)
            parent_ids[local_id] = identity
            parents.append(VersionChunk(**asdict(source), chunk_id=identity, page_number=page.page_number,
                                        order=order, text=raw.page_content))
        for raw in raw_children:
            order = len(children) + 1
            identity = chunk_id(fingerprint, "child", page.page_number, order, raw.page_content)
            children.append(VersionChunk(**asdict(source), chunk_id=identity, page_number=page.page_number,
                                         order=order, text=raw.page_content, parent_id=parent_ids[raw.metadata["parent_id"]]))
        if len(parents) + len(children) > MAX_CHUNKS:
            raise ChunkingError("chunk_resource_limit")
    result = ChunkedVersion(binding, config, parsed.page_count, tuple(blanks), tuple(parents), tuple(children))
    validate_chunks(result, binding, config)
    return result
