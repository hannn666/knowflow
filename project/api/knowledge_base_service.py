from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from project.db.business_models import KnowledgeBase


def create_knowledge_base(session: Session, owner_id: UUID, name: str) -> KnowledgeBase:
    # owner_id must come from authenticated identity, never request JSON.
    knowledge_base = KnowledgeBase(owner_id=owner_id, name=name)
    session.add(knowledge_base)
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
    session.refresh(knowledge_base)
    return knowledge_base


def list_knowledge_bases(
    session: Session, owner_id: UUID, limit: int, offset: int,
) -> list[KnowledgeBase]:
    return list(session.scalars(
        select(KnowledgeBase)
        .where(KnowledgeBase.owner_id == owner_id)
        .order_by(KnowledgeBase.created_at, KnowledgeBase.id)
        .limit(limit).offset(offset)
    ))


def get_owned_knowledge_base(
    session: Session, owner_id: UUID, knowledge_base_id: UUID,
) -> KnowledgeBase | None:
    return session.scalar(select(KnowledgeBase).where(
        KnowledgeBase.id == knowledge_base_id,
        KnowledgeBase.owner_id == owner_id,
    ))
