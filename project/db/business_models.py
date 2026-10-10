from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Uuid, func, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    email: Mapped[str] = mapped_column(
        String(320), unique=True, nullable=False
    )
    password_hash: Mapped[str] = mapped_column(
        String(255), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    owner_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(
        String(200), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    # Let foreign keys reject parent deletion; never null or delete children.
    documents: Mapped[list["Document"]] = relationship(
        back_populates="knowledge_base", passive_deletes="all",
    )


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    knowledge_base_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("knowledge_bases.id"),
        nullable=False, index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    knowledge_base: Mapped[KnowledgeBase] = relationship(back_populates="documents")
    versions: Mapped[list["DocumentVersion"]] = relationship(
        back_populates="document", passive_deletes="all",
    )


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'processing', 'ready', 'failed')",
            name="ck_document_versions_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    document_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id"), nullable=False, index=True,
    )
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    # ORM inserts and SQL that omits status both start pending. Explicit SQL
    # NULL is rejected; ready describes processing, not retrieval authorization.
    status: Mapped[str] = mapped_column(
        String(20), default="pending", server_default=text("'pending'"), nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    document: Mapped[Document] = relationship(back_populates="versions")


class DocumentParseStage(Base):
    """One authoritative parse-stage record per version; no overall-state writes."""
    __tablename__ = "document_parse_stages"
    __table_args__ = (
        CheckConstraint("status IN ('running', 'succeeded', 'failed')", name="ck_parse_stage_status"),
        CheckConstraint(
            "(status = 'running' AND page_count IS NULL AND has_text IS NULL "
            "AND result_sha256 IS NULL AND error_code IS NULL AND finished_at IS NULL) OR "
            "(status = 'succeeded' AND page_count IS NOT NULL AND page_count > 0 AND has_text IS NOT NULL "
            "AND result_sha256 IS NOT NULL AND length(result_sha256) = 64 "
            "AND error_code IS NULL AND finished_at IS NOT NULL) OR "
            "(status = 'failed' AND page_count IS NULL AND has_text IS NULL "
            "AND result_sha256 IS NULL AND error_code IS NOT NULL AND finished_at IS NOT NULL)",
            name="ck_parse_stage_result",
        ),
    )

    document_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("document_versions.id"), primary_key=True,
    )
    attempt_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'running'"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    has_text: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    result_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
