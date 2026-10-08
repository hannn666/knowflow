from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from project.api.auth_dependencies import get_current_user
from project.api.knowledge_base_schemas import KnowledgeBaseCreate, KnowledgeBaseResponse
from project.api.knowledge_base_service import (
    create_knowledge_base, get_owned_knowledge_base, list_knowledge_bases,
)
from project.db.business_database import get_database_session
from project.db.business_models import User


router = APIRouter(prefix="/knowledge-bases", tags=["knowledge-bases"])
CurrentUser = Annotated[User, Depends(get_current_user)]
DatabaseSession = Annotated[Session, Depends(get_database_session)]


@router.post("", response_model=KnowledgeBaseResponse, status_code=201)
def create(
    request: KnowledgeBaseCreate, response: Response,
    user: CurrentUser, session: DatabaseSession,
) -> KnowledgeBaseResponse:
    result = create_knowledge_base(session, user.id, request.name)
    response.headers["Cache-Control"] = "no-store"
    return KnowledgeBaseResponse.model_validate(result)


@router.get("", response_model=list[KnowledgeBaseResponse])
def list_owned(
    response: Response, user: CurrentUser, session: DatabaseSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[KnowledgeBaseResponse]:
    response.headers["Cache-Control"] = "no-store"
    return [KnowledgeBaseResponse.model_validate(item) for item in
            list_knowledge_bases(session, user.id, limit, offset)]


@router.get("/{knowledge_base_id}", response_model=KnowledgeBaseResponse)
def get_one(
    knowledge_base_id: UUID, response: Response,
    user: CurrentUser, session: DatabaseSession,
) -> KnowledgeBaseResponse:
    result = get_owned_knowledge_base(session, user.id, knowledge_base_id)
    if result is None:
        # Do not disclose whether another user's record exists.
        raise HTTPException(404, "Knowledge base not found",
                            headers={"Cache-Control": "no-store"})
    response.headers["Cache-Control"] = "no-store"
    return KnowledgeBaseResponse.model_validate(result)
