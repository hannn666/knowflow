"""Internal parsing entry point, for server-authenticated workflows only.

No HTTP route is registered. owner_id must originate from get_current_user or a
trusted server job, not a client-supplied owner claim. Source columns are loaded
from the database under the full ownership chain; no arbitrary path is accepted.
"""
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from project.api.document_service import DocumentNotFound
from project.core.document_parser import DocumentVersionSource, ParsedPdf, parse_pdf_version
from project.core.document_storage import DocumentStorage
from project.db.business_models import Document, DocumentVersion, KnowledgeBase


def parse_document_version(session: Session, owner_id: UUID, knowledge_base_id: UUID,
                           document_id: UUID, version_id: UUID, storage: DocumentStorage) -> ParsedPdf:
    # Column selection reads persisted source values even if ORM instances in
    # the caller's identity map are dirty. Do not flush their unrelated edits.
    with session.no_autoflush:
        source = session.execute(select(
            KnowledgeBase.id, Document.id, DocumentVersion.id, DocumentVersion.original_filename,
        ).select_from(DocumentVersion).join(Document).join(KnowledgeBase).where(
            KnowledgeBase.owner_id == owner_id, KnowledgeBase.id == knowledge_base_id,
            Document.knowledge_base_id == knowledge_base_id, Document.id == document_id,
            DocumentVersion.document_id == document_id, DocumentVersion.id == version_id,
        )).one_or_none()
    if source is None:
        raise DocumentNotFound
    return parse_pdf_version(DocumentVersionSource(*source), storage)
