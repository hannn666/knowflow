from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from project.api.passwords import hash_password
from project.db.business_models import User


class EmailAlreadyRegistered(Exception):
    pass


def register_user(session: Session, email: str, password: str) -> User:
    user = User(email=email, password_hash=hash_password(password))
    session.add(user)
    try:
        # The database unique constraint also protects concurrent requests.
        session.commit()
    except IntegrityError:
        session.rollback()
        existing_id = session.scalar(select(User.id).where(User.email == email))
        if existing_id is not None:
            raise EmailAlreadyRegistered from None
        # Do not misreport an unrelated database failure as a duplicate email.
        raise
    session.refresh(user)
    return user
