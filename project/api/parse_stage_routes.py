from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from project.api.auth_dependencies import get_current_user
from project.api.document_service import DocumentNotFound
from project.api.parse_stage_schemas import ParseStageResponse
from project.api.parse_stage_service import ParseStageBusy, get_parse_stage, trigger_parse_stage
from project.api.upload_limits import NO_STORE
from project.core.document_storage import DocumentStorage, get_document_storage
from project.core.parse_executor import BoundedParseExecutor, ParseCapacityUnavailable, get_parse_executor
from project.db.business_database import get_database_session
from project.db.business_models import User


router = APIRouter(prefix='/knowledge-bases', tags=['document-parsing'])
PATH = '/{knowledge_base_id}/documents/{document_id}/versions/{version_id}/parse'
CurrentUser = Annotated[User, Depends(get_current_user)]
DatabaseSession = Annotated[Session, Depends(get_database_session)]
Storage = Annotated[DocumentStorage, Depends(get_document_storage)]
Executor = Annotated[BoundedParseExecutor, Depends(get_parse_executor)]


def translate_error(error):
    if isinstance(error, DocumentNotFound):
        return HTTPException(404, 'Document resource not found', headers=NO_STORE)
    if isinstance(error, ParseStageBusy):
        return HTTPException(409, 'Parsing is already running or changed concurrently', headers=NO_STORE)
    if isinstance(error, ParseCapacityUnavailable):
        return HTTPException(503, 'Parser capacity is currently exhausted', headers=NO_STORE)
    return HTTPException(503, 'Parsing stage unavailable', headers=NO_STORE)


@router.post(PATH, response_model=ParseStageResponse)
async def trigger(knowledge_base_id: UUID, document_id: UUID, version_id: UUID,
                  request: Request, response: Response, user: CurrentUser,
                  session: DatabaseSession, storage: Storage, executor: Executor):
    # No path/options/text/body supplied by the client is consumed as input.
    async for chunk in request.stream():
        if chunk:
            raise HTTPException(422, 'Parsing trigger does not accept a request body', headers=NO_STORE)
    response.headers.update(NO_STORE)
    try:
        return await run_in_threadpool(trigger_parse_stage, session, user.id, knowledge_base_id,
                                       document_id, version_id, storage, executor)
    except Exception as error:
        raise translate_error(error) from None


@router.get(PATH, response_model=ParseStageResponse)
def query(knowledge_base_id: UUID, document_id: UUID, version_id: UUID,
          response: Response, user: CurrentUser, session: DatabaseSession, storage: Storage):
    response.headers.update(NO_STORE)
    try:
        return get_parse_stage(session, user.id, knowledge_base_id, document_id, version_id, storage)
    except Exception as error:
        raise translate_error(error) from None
