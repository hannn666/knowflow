from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DataError, IntegrityError

from project.db.business_models import Document, DocumentVersion, KnowledgeBase, User


@pytest.fixture
def document(db_session):
    owner = User(email=f"document-{uuid4().hex}@example.com", password_hash="test-only")
    db_session.add(owner)
    db_session.flush()
    knowledge_base = KnowledgeBase(owner_id=owner.id, name="Document tests")
    item = Document(knowledge_base=knowledge_base)
    db_session.add(item)
    db_session.commit()
    return item


def test_relationships_survive_reload(db_session, document):
    version = DocumentVersion(document=document, original_filename="员工手册.pdf")
    db_session.add(version)
    db_session.commit()
    version_id, document_id, kb_id = version.id, document.id, document.knowledge_base_id
    db_session.expunge_all()
    saved = db_session.get(DocumentVersion, version_id)
    assert saved.document.id == document_id
    assert saved.document.knowledge_base.id == kb_id
    assert saved in saved.document.versions
    assert saved.document in saved.document.knowledge_base.documents
    assert db_session.get(User, saved.document.knowledge_base.owner_id) is not None
    assert isinstance(saved.id, UUID)
    assert saved.created_at is not None
    assert saved.document.created_at is not None


def test_new_version_preserves_old_version(db_session, document):
    first = DocumentVersion(document=document, original_filename="手册旧版.pdf", status="ready")
    db_session.add(first)
    db_session.commit()
    old_values = (first.id, first.original_filename, first.status, first.created_at)
    second = DocumentVersion(document=document, original_filename="手册新版.pdf")
    db_session.add(second)
    db_session.commit()
    document_id, second_id = document.id, second.id
    db_session.expunge_all()
    versions = list(db_session.scalars(select(DocumentVersion).where(
        DocumentVersion.document_id == document_id)))
    assert len(versions) == 2
    assert {item.id for item in versions} == {old_values[0], second_id}
    saved = db_session.get(DocumentVersion, old_values[0])
    assert (saved.id, saved.original_filename, saved.status, saved.created_at) == old_values


def test_same_filename_does_not_merge_documents(db_session, document):
    other = Document(knowledge_base=document.knowledge_base)
    db_session.add_all([
        DocumentVersion(document=document, original_filename="manual.pdf"),
        DocumentVersion(document=other, original_filename="manual.pdf"),
    ])
    db_session.commit()
    db_session.expire_all()
    assert document.id != other.id
    assert document.versions[0].original_filename == other.versions[0].original_filename
    assert document.versions[0].id != other.versions[0].id


def test_orm_default_status_is_pending(db_session, document):
    version = DocumentVersion(document=document, original_filename="manual.pdf")
    db_session.add(version)
    db_session.commit()
    db_session.refresh(version)
    assert version.status == "pending"


def test_direct_sql_omitted_status_uses_database_default(db_session, document):
    version_id = uuid4()
    # UUIDs, unlike status and created_at, have Python defaults only.
    db_session.execute(text(
        "INSERT INTO document_versions (id, document_id, original_filename) "
        "VALUES (:id, :document_id, :filename)"
    ), {"id": version_id.hex, "document_id": document.id.hex, "filename": "sql.pdf"})
    db_session.commit()
    saved = db_session.get(DocumentVersion, version_id)
    assert saved.status == "pending"
    assert saved.created_at is not None


@pytest.mark.parametrize("status", ["pending", "processing", "ready", "failed"])
def test_each_valid_status_persists(db_session, document, status):
    version = DocumentVersion(document=document, original_filename="manual.pdf", status=status)
    db_session.add(version)
    db_session.commit()
    db_session.refresh(version)
    assert version.status == status


@pytest.mark.parametrize("status", ["unknown", "READY", ""])
def test_database_rejects_invalid_status(db_session, document, status):
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.execute(text(
                "INSERT INTO document_versions (id, document_id, original_filename, status) "
                "VALUES (:id, :document_id, 'manual.pdf', :status)"
            ), {"id": uuid4().hex, "document_id": document.id.hex, "status": status})


@pytest.mark.parametrize("table,field", [
    ("documents", "knowledge_base_id"), ("documents", "created_at"),
    ("document_versions", "document_id"), ("document_versions", "original_filename"),
    ("document_versions", "status"), ("document_versions", "created_at"),
])
def test_database_rejects_explicit_null(db_session, document, table, field):
    values = {"id": uuid4().hex}
    if table == "documents":
        values["knowledge_base_id"] = document.knowledge_base_id.hex
    else:
        values.update(document_id=document.id.hex, original_filename="manual.pdf", status="pending")
    values[field] = None
    # Table/column names come exclusively from the parametrization above.
    columns = ", ".join(values)
    placeholders = ", ".join(f":{key}" for key in values)
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.execute(text(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})"), values)


@pytest.mark.parametrize("table,field", [
    ("documents", "knowledge_base_id"), ("document_versions", "document_id"),
])
def test_database_rejects_missing_parent(db_session, table, field):
    if db_session.bind.dialect.name == "sqlite":
        assert db_session.scalar(text("PRAGMA foreign_keys")) == 1
    values = {"id": uuid4().hex, field: uuid4().hex}
    if table == "document_versions":
        values["original_filename"] = "manual.pdf"
    columns = ", ".join(values)
    placeholders = ", ".join(f":{key}" for key in values)
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.execute(text(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})"), values)


def test_original_filename_declares_length_255():
    assert DocumentVersion.__table__.c.original_filename.type.length == 255


def test_postgres_rejects_filename_over_255_characters(db_session, document):
    # Only PostgreSQL can establish this database behavior; the standalone
    # verifier runs it against its disposable PostgreSQL database.
    if db_session.bind.dialect.name != "postgresql":
        pytest.skip("Requires PostgreSQL; run verify_document_schema_postgres.py")
    accepted = DocumentVersion(document=document, original_filename="文" * 255)
    db_session.add(accepted)
    db_session.commit()
    with pytest.raises(DataError):
        with db_session.begin_nested():
            db_session.execute(text(
                "INSERT INTO document_versions (id, document_id, original_filename) "
                "VALUES (:id, :document_id, :filename)"
            ), {"id": uuid4().hex, "document_id": document.id.hex, "filename": "文" * 256})


@pytest.mark.parametrize("parent_type", ["document", "knowledge_base"])
def test_relationships_do_not_delete_or_null_children(db_session, document, parent_type):
    version = DocumentVersion(document=document, original_filename="manual.pdf")
    db_session.add(version)
    db_session.commit()
    version_id, document_id = version.id, document.id
    # Load collections as well, proving ORM does not null children on delete.
    assert document.versions
    assert document.knowledge_base.documents
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.delete(document if parent_type == "document" else document.knowledge_base)
            db_session.flush()
    assert db_session.get(DocumentVersion, version_id).document_id == document_id
