from __future__ import annotations

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.core.security import hash_password
from app.models import User
from app.services.audit import audit


async def create_admin(email: str, password: str) -> bool:
    """Create the first administrator. Returns False if the email already exists."""
    async with get_sessionmaker()() as db:
        existing = await db.scalar(select(User).where(User.email == email.lower()))
        if existing is not None:
            return False
        user = User(email=email.lower(), display_name="Administrator", password_hash=hash_password(password))
        user.is_admin = True
        db.add(user)
        await db.flush()
        audit(db, "admin.create_via_cli", resource=f"user:{user.id}")
        await db.commit()
        return True
