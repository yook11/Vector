from datetime import UTC, datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, UniqueConstraint
from sqlmodel import Field, Relationship, SQLModel


class UserKeywordSubscription(SQLModel, table=True):
    __tablename__ = "user_keyword_subscriptions"
    __table_args__ = (
        UniqueConstraint("user_id", "keyword_id", name="uq_user_keyword"),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    keyword_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("keywords.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        nullable=False,
        sa_type=DateTime(timezone=True),
    )

    # Relationships
    user: "User" = Relationship(back_populates="subscriptions")
    keyword: "Keyword" = Relationship(back_populates="user_subscriptions")


# Resolve forward references
from app.models.keyword import Keyword  # noqa: E402, F811
from app.models.user import User  # noqa: E402, F811

UserKeywordSubscription.model_rebuild()
