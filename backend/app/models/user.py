from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import DateTime
from sqlmodel import Field, Relationship, SQLModel


class UserRole(StrEnum):
    USER = "user"
    ADMIN = "admin"


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: int | None = Field(default=None, primary_key=True)
    email: str = Field(max_length=255, unique=True, nullable=False, index=True)
    hashed_password: str = Field(max_length=255, nullable=False)
    display_name: str | None = Field(default=None, max_length=100)
    role: UserRole = Field(default=UserRole.USER, max_length=20, nullable=False)
    is_active: bool = Field(default=True, nullable=False)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        nullable=False,
        sa_type=DateTime(timezone=True),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        nullable=False,
        sa_type=DateTime(timezone=True),
    )

    # Relationships
    refresh_tokens: list["RefreshToken"] = Relationship(back_populates="user")
    subscriptions: list["UserKeywordSubscription"] = Relationship(back_populates="user")
    watchlist_items: list["WatchlistItem"] = Relationship(back_populates="user")


# Resolve forward references
from app.models.refresh_token import RefreshToken  # noqa: E402, F811
from app.models.user_keyword import UserKeywordSubscription  # noqa: E402, F811
from app.models.watchlist import WatchlistItem  # noqa: E402, F811

User.model_rebuild()
