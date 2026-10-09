from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, UploadFile
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from project.api.auth_dependencies import get_current_user
from project.api.document_schemas import DocumentVersionResponse
from project.api.document_service import DocumentNotFound, get_document_version, upload_document
from project.api.upload_limits import DocumentUploadRoute, NO_STORE
from project.core.document_storage import DocumentStorage, UploadRejected, get_document_storage
from project.db.business_database import get_database_session
from project.db.business_models import User


router = APIRouter(prefix="/knowledge-bases", tags=["documents"], route_class=DocumentUploadRoute)
CurrentUser = Annotated[User, Depends(get_current_user)]
DatabaseSession = Annotated[Session, Depends(get_database_session)]
Storage = Annotated[DocumentStorage, Depends(get_document_storage)]


def not_found() -> HTTPException:
    return HTTPException(404, "Document resource not found", headers=NO_STORE)


@router.post("/{knowledge_base_id}/documents", response_model=DocumentVersionResponse, status_code=201)
async def upload(knowledge_base_id: UUID, request: Request, response: Response,
                 file: UploadFile, user: CurrentUser, session: DatabaseSession,
                 storage: Storage) -> DocumentVersionResponse:
    response.headers.update(NO_STORE)
    form = await request.form()
    if len(form.multi_items()) != 1 or form.multi_items()[0][0] != "file":
        raise HTTPException(422, "Provide exactly one file and no other fields", headers=NO_STORE)
    try:
        return await run_in_threadpool(upload_document, session, user.id, knowledge_base_id, file, storage)
    except DocumentNotFound:
        raise not_found() from None
    except UploadRejected as error:
        raise HTTPException(error.status_code, error.detail, headers=NO_STORE) from None
    except Exception:
        raise HTTPException(503, "Unable to save document", headers=NO_STORE) from None


@router.get("/{knowledge_base_id}/documents/{document_id}/versions/{version_id}",
            response_model=DocumentVersionResponse)
def get_version(knowledge_base_id: UUID, document_id: UUID, version_id: UUID,
                response: Response, user: CurrentUser,
                session: DatabaseSession) -> DocumentVersionResponse:
    response.headers.update(NO_STORE)
    try:
        return get_document_version(session, user.id, knowledge_base_id, document_id, version_id)
    except DocumentNotFound:
        raise not_found() from None
