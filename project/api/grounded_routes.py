"""Authenticated index/ask routes, with bounded bodies and no-store responses."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from project.api.auth_dependencies import get_current_user
from project.api.document_service import DocumentNotFound
from project.api.grounded_schemas import AskRequest, AskResponse, IndexResponse
from project.api.grounded_service import GroundedService, QABusy, get_grounded_service
from project.api.upload_limits import NO_STORE
from project.core.document_storage import DocumentStorage, get_document_storage
from project.core.index_models import IndexingError
from project.core.grounded_llm import PromptBudgetExceeded
from project.core.version_chunker import ChunkingError
from project.db.business_database import get_database_session
from project.db.business_models import User

MAX_BODY_BYTES = 16 * 1024


class GroundedRoute(APIRoute):
    async def handle(self, scope, receive, send):
        received=0
        started=False
        async def limited_receive():
            nonlocal received
            message=await receive()
            if message["type"]=="http.request":
                received+=len(message.get("body",b""))
                if received>MAX_BODY_BYTES:
                    raise HTTPException(413,"Request body too large",headers=NO_STORE)
            return message
        async def private_send(message):
            nonlocal started
            if message["type"]=="http.response.start":
                started=True
                headers=[(k,v) for k,v in message["headers"] if k.lower()!=b"cache-control"]
                message={**message,"headers":headers+[(b"cache-control",b"no-store")]}
            await send(message)
        try:
            await super().handle(scope,limited_receive,private_send)
        except Exception:
            if started:
                raise  # Never send a second response on a broken connection.
            # Includes dependency initialization failures before endpoint code.
            await JSONResponse({"detail":"Document service unavailable"},503,
                               headers=NO_STORE)(scope,receive,private_send)


router=APIRouter(prefix="/knowledge-bases",tags=["document-grounded-qa"],route_class=GroundedRoute)
PATH="/{knowledge_base_id}/documents/{document_id}/versions/{version_id}"
CurrentUser=Annotated[User,Depends(get_current_user)]
DatabaseSession=Annotated[Session,Depends(get_database_session)]
Storage=Annotated[DocumentStorage,Depends(get_document_storage)]
Service=Annotated[GroundedService,Depends(get_grounded_service)]


def translate(error):
    if isinstance(error,DocumentNotFound):
        return HTTPException(404,"Document resource not found",headers=NO_STORE)
    if isinstance(error,PromptBudgetExceeded):
        return HTTPException(503,"Question answering input exceeds the resource budget",headers=NO_STORE)
    if isinstance(error,QABusy):
        return HTTPException(503,"Document processing capacity is exhausted",headers=NO_STORE)
    if isinstance(error,ChunkingError) or (isinstance(error,IndexingError) and
            (error.code.startswith("source_") or error.code in {"no_indexable_text","index_verification_failed",
                "index_point_identity_conflict","index_collection_mismatch","index_resource_limit"})):
        return HTTPException(409,"Document input or index is not valid for this operation",headers=NO_STORE)
    return HTTPException(503,"Document question answering service unavailable",headers=NO_STORE)


@router.post(PATH+"/index",response_model=IndexResponse)
async def index(knowledge_base_id:UUID,document_id:UUID,version_id:UUID,request:Request,
                user:CurrentUser,session:DatabaseSession,storage:Storage,service:Service):
    async for chunk in request.stream():
        if chunk:
            raise HTTPException(422,"Index trigger does not accept a request body",headers=NO_STORE)
    try:
        return await run_in_threadpool(service.index_version,session,user.id,knowledge_base_id,document_id,version_id,storage)
    except Exception as error:
        raise translate(error) from None


@router.post(PATH+"/ask",response_model=AskResponse)
async def ask(knowledge_base_id:UUID,document_id:UUID,version_id:UUID,body:AskRequest,
              user:CurrentUser,session:DatabaseSession,storage:Storage,service:Service):
    try:
        return await run_in_threadpool(service.ask,session,user.id,knowledge_base_id,document_id,version_id,storage,body.question)
    except Exception as error:
        raise translate(error) from None
