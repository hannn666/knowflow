from sqlalchemy import UniqueConstraint

from project.db.business_models import Base


def test_business_tables_are_registered() -> None:
    assert set(Base.metadata.tables) == {
        "users", "knowledge_bases", "documents", "document_versions", "document_parse_stages",
    }


def test_user_email_is_unique() -> None:
    users = Base.metadata.tables["users"]
    assert users.c.email.nullable is False
    assert any(
        isinstance(constraint, UniqueConstraint)
        and tuple(constraint.columns.keys()) == ("email",)
        for constraint in users.constraints
    )


def test_knowledge_base_declares_owner_foreign_key() -> None:
    knowledge_bases = Base.metadata.tables["knowledge_bases"]
    owner_id = knowledge_bases.c.owner_id

    assert owner_id.nullable is False
    assert {fk.target_fullname for fk in owner_id.foreign_keys} == {"users.id"}
    assert any(
        tuple(index.columns.keys()) == ("owner_id",)
        for index in knowledge_bases.indexes
    )
