from datetime import UTC, datetime
from typing import Optional

from sqlalchemy import DateTime
from sqlmodel import Field, Relationship, SQLModel


class RefreshToken(SQLModel, table=True):
    __tablename__ = "refresh_tokens"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", nullable=False, index=True)
    token_hash: str = Field(max_length=255, unique=True, nullable=False)
    expires_at: datetime = Field(nullable=False, sa_type=DateTime(timezone=True))
    is_revoked: bool = Field(default=False, nullable=False)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        nullable=False,
        sa_type=DateTime(timezone=True),
    )

    # Relationships
    user: Optional["User"] = Relationship(back_populates="refresh_tokens")


# Resolve forward reference
from app.models.user import User  # noqa: E402, F811

RefreshToken.model_rebuild()
