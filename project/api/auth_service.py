from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from project.api.passwords import hash_password, verify_password
from project.db.business_models import User


class EmailAlreadyRegistered(Exception):
    pass


# Unknown accounts still perform password verification to reduce timing leaks.
_DUMMY_HASH = hash_password("not a real account password")


def authenticate_user(session: Session, email: str, password: str) -> User | None:
    user = session.scalar(select(User).where(User.email == email))
    password_hash = user.password_hash if user is not None else _DUMMY_HASH
    valid = verify_password(password, password_hash)
    if user is None or not valid:
        return None
    return user


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
