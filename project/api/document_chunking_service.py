"""Internal service for server-authenticated identities; no HTTP entry point.

owner_id must come from authentication/trusted server context, never a client
claim. Inputs are SQL-authorized and tied to the currently successful parse
attempt/digest. Consumers must use the read service again before later indexing.
No commit/flush, parse-state update or overall-version-state write occurs here.
"""
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from project.api.document_parsing_service import get_authorized_parse_source
from project.api.document_service import DocumentNotFound
from project.core.chunk_artifacts import ChunkArtifactStore
from project.core.parse_artifacts import ParseArtifactError, ParseArtifactStore
from project.core.version_chunker import ChunkingConfig, ChunkingError, ParseBinding, chunk_parsed_version
from project.db.business_models import DocumentParseStage


def _inputs(session, owner_id, kb_id, document_id, version_id, storage):
    try:
        source = get_authorized_parse_source(session, owner_id, kb_id, document_id, version_id)
        with session.no_autoflush:
            record = session.execute(select(
                DocumentParseStage.status, DocumentParseStage.attempt_id, DocumentParseStage.result_sha256,
                DocumentParseStage.page_count, DocumentParseStage.has_text,
            ).where(DocumentParseStage.document_version_id == version_id)).one_or_none()
        if record is None or record.status != "succeeded":
            raise ChunkingError("parse_not_succeeded")
        binding = ParseBinding(source, record.attempt_id, record.result_sha256)
        parsed = ParseArtifactStore(storage).read(source, binding.attempt_id, binding.sha256)
        if parsed.page_count != record.page_count or parsed.has_text is not record.has_text:
            raise ChunkingError("parse_artifact_invalid")
        return binding, parsed
    except DocumentNotFound:
        raise
    except ParseArtifactError as error:
        raise ChunkingError("parse_" + error.code) from None
    except SQLAlchemyError:
        raise ChunkingError("chunking_unavailable") from None


def _verify_current(session, owner_id, kb_id, document_id, version_id, storage, binding):
    current, _ = _inputs(session, owner_id, kb_id, document_id, version_id, storage)
    if current != binding:
        raise ChunkingError("parse_input_changed")


def generate_document_version_chunks(session, owner_id, knowledge_base_id, document_id,
                                     version_id, storage, config=None):
    config = ChunkingConfig() if config is None else config
    binding, parsed = _inputs(session, owner_id, knowledge_base_id, document_id, version_id, storage)
    store = ChunkArtifactStore(storage)
    try:
        result = store.read(binding, config)
    except ChunkingError as error:
        if error.code != "chunk_artifact_missing":
            raise
        result = chunk_parsed_version(parsed, binding, config)
        _verify_current(session, owner_id, knowledge_base_id, document_id, version_id, storage, binding)
        result = store.publish(result)
    _verify_current(session, owner_id, knowledge_base_id, document_id, version_id, storage, binding)
    return result


def read_document_version_chunks(session, owner_id, knowledge_base_id, document_id,
                                 version_id, storage, config=None):
    config = ChunkingConfig() if config is None else config
    binding, _ = _inputs(session, owner_id, knowledge_base_id, document_id, version_id, storage)
    result = ChunkArtifactStore(storage).read(binding, config)
    _verify_current(session, owner_id, knowledge_base_id, document_id, version_id, storage, binding)
    return result
