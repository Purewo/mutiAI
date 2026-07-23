"""Development bootstrap records created after database migration."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from mutiai.config import Settings
from mutiai.models import User
from mutiai.security import hash_password


def seed_development_admin(session: Session, settings: Settings) -> User:
    """Create the configured local admin once without resetting its password."""

    username = settings.bootstrap_admin_username
    existing = session.scalar(select(User).where(User.username == username))
    if existing is not None:
        return existing

    user = User(
        username=username,
        password_hash=hash_password(
            settings.bootstrap_admin_password.get_secret_value()
        ),
        display_name="Administrator",
        is_active=True,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user
