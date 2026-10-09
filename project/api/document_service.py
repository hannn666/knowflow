import logging
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from project.api.document_schemas import DocumentVersionResponse
from project.api.knowledge_base_service import get_owned_knowledge_base
from project.core.document_storage import DocumentStorage, validate_filename
from project.db.business_models import Document, DocumentVersion, KnowledgeBase


logger = logging.getLogger(__name__)


class DocumentNotFound(Exception):
    pass


def response_for(version: DocumentVersion, knowledge_base_id: UUID) -> DocumentVersionResponse:
    return DocumentVersionResponse(
        knowledge_base_id=knowledge_base_id, document_id=version.document_id,
        document_version_id=version.id, original_filename=version.original_filename,
        status=version.status, created_at=version.created_at,
    )


def rollback_safely(session: Session) -> None:
    try:
        session.rollback()
    except Exception:
        logger.warning("Upload transaction rollback failed")


def upload_document(session: Session, owner_id: UUID, knowledge_base_id: UUID,
                    file: UploadFile, storage: DocumentStorage) -> DocumentVersionResponse:
    if get_owned_knowledge_base(session, owner_id, knowledge_base_id) is None:
        raise DocumentNotFound
    filename = validate_filename(file.filename, file.content_type)
    document = Document(id=uuid4(), knowledge_base_id=knowledge_base_id)
    version_id = uuid4()
    version = DocumentVersion(id=version_id, document=document, original_filename=filename)
    saved_path = None
    try:
        session.add(document)
        session.flush()
        saved_path = storage.save(file.file, knowledge_base_id, document.id, version.id)
        # Materialize all fields before commit; no post-commit DB refresh.
        result = response_for(version, knowledge_base_id)
    except BaseException:
        rollback_safely(session)
        if saved_path is not None:
            storage.discard(saved_path)
        raise
    try:
        session.commit()
    except IntegrityError:
        # A constraint rejection explicitly establishes a failed transaction.
        rollback_safely(session)
        storage.discard(saved_path)
        raise
    except BaseException:
        # A lost commit acknowledgement can hide a successful commit. Preserve
        # the file, even after local rollback, for future scoped reconciliation.
        rollback_safely(session)
        logger.warning("Upload commit outcome uncertain for version %s; original file retained", version_id)
        raise
    return result


def get_document_version(session: Session, owner_id: UUID, knowledge_base_id: UUID,
                         document_id: UUID, version_id: UUID) -> DocumentVersionResponse:
    version = session.scalar(select(DocumentVersion).join(Document).join(KnowledgeBase).where(
        KnowledgeBase.owner_id == owner_id, KnowledgeBase.id == knowledge_base_id,
        Document.knowledge_base_id == knowledge_base_id, Document.id == document_id,
        DocumentVersion.document_id == document_id, DocumentVersion.id == version_id,
    ))
    if version is None:
        raise DocumentNotFound
    return response_for(version, knowledge_base_id)
