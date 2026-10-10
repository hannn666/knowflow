"""Immutable, version/input/config scoped chunk JSON on trusted local storage."""
from dataclasses import asdict
import hashlib
import json
import logging
import os
from pathlib import Path
from uuid import UUID, uuid4

from project.core.document_storage import DocumentStorage
from project.core.version_chunker import (
    ALGORITHM, ChunkedVersion, ChunkingConfig, ChunkingError, ParseBinding,
    VersionChunk, canonical_json, validate_chunks,
)

MAX_CHUNK_ARTIFACT_BYTES = 32 * 1024 * 1024
logger = logging.getLogger(__name__)


class ChunkArtifactStore:
    def __init__(self, storage: DocumentStorage):
        self.storage = storage

    def path_for(self, binding: ParseBinding, config: ChunkingConfig) -> Path:
        try:
            source = binding.source
            original = self.storage.path_for(source.knowledge_base_id, source.document_id, source.document_version_id)
            path = original.parent / "chunking" / binding.fingerprint(config) / "chunks.json"
            for component in (path, *path.parents):
                if component == self.storage.root:
                    break
                if component.is_symlink() or getattr(component, "is_junction", lambda: False)():
                    raise ChunkingError("chunk_artifact_unavailable")
            if not path.resolve().is_relative_to(self.storage.root):
                raise ChunkingError("chunk_artifact_unavailable")
            return path
        except (OSError, ValueError):
            raise ChunkingError("chunk_artifact_unavailable") from None

    @staticmethod
    def _payload(result: ChunkedVersion):
        return {"algorithm": ALGORITHM, **asdict(result)}

    def read(self, binding: ParseBinding, config: ChunkingConfig) -> ChunkedVersion:
        try:
            with self.path_for(binding, config).open("rb") as input_file:
                encoded = input_file.read(MAX_CHUNK_ARTIFACT_BYTES + 1)
            if len(encoded) > MAX_CHUNK_ARTIFACT_BYTES:
                raise ChunkingError("chunk_resource_limit")
            envelope = json.loads(encoded)
            payload = envelope["payload"]
            if (type(envelope["schema_version"]) is not int or envelope["schema_version"] != 1
                    or hashlib.sha256(canonical_json(payload)).hexdigest() != envelope["sha256"]
                    or payload["algorithm"] != ALGORITHM
                    or payload["binding"] != json.loads(canonical_json(asdict(binding)))
                    or payload["config"] != json.loads(canonical_json(asdict(config)))):
                raise ChunkingError("chunk_artifact_invalid")
            def decode(item):
                item = dict(item)
                for name in ("knowledge_base_id", "document_id", "document_version_id", "chunk_id"):
                    item[name] = UUID(item[name])
                if item["parent_id"] is not None:
                    item["parent_id"] = UUID(item["parent_id"])
                return VersionChunk(**item)
            result = ChunkedVersion(binding, config, payload["page_count"], tuple(payload["blank_page_numbers"]),
                                    tuple(decode(item) for item in payload["parents"]),
                                    tuple(decode(item) for item in payload["children"]))
            validate_chunks(result, binding, config)
            return result
        except ChunkingError:
            raise
        except FileNotFoundError:
            raise ChunkingError("chunk_artifact_missing") from None
        except (OSError, UnicodeError):
            raise ChunkingError("chunk_artifact_unavailable") from None
        except Exception:
            raise ChunkingError("chunk_artifact_invalid") from None

    def publish(self, result: ChunkedVersion) -> ChunkedVersion:
        validate_chunks(result, result.binding, result.config)
        payload = self._payload(result)
        encoded = canonical_json({"schema_version": 1, "payload": payload,
                                  "sha256": hashlib.sha256(canonical_json(payload)).hexdigest()})
        if len(encoded) > MAX_CHUNK_ARTIFACT_BYTES:
            raise ChunkingError("chunk_resource_limit")
        final = self.path_for(result.binding, result.config)
        temporary = final.with_name(f".chunks-{uuid4().hex}.part")
        owned = False
        try:
            final.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("xb") as output:
                owned = True
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            # Same-directory hard link publishes a complete file atomically and
            # never replaces an existing winner, even across local processes.
            try:
                os.link(temporary, final)
            except FileExistsError:
                pass
            cached = self.read(result.binding, result.config)
            if cached != result:
                raise ChunkingError("chunk_artifact_conflict")
            return cached
        except ChunkingError:
            raise
        except OSError:
            raise ChunkingError("chunk_artifact_write_failed") from None
        finally:
            if owned:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Owned chunk temporary file cleanup incomplete")
