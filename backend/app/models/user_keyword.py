from datetime import UTC, datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlmodel import Field, Relationship, SQLModel


class UserKeywordSubscription(SQLModel, table=True):
    __tablename__ = "user_keyword_subscriptions"
    __table_args__ = (
        UniqueConstraint("user_id", "keyword_id", name="uq_user_keyword"),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: str = Field(sa_column=Column(String(32), nullable=False, index=True))
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
    keyword: "Keyword" = Relationship(back_populates="user_subscriptions")


# Resolve forward references
from app.models.keyword import Keyword  # noqa: E402

UserKeywordSubscription.model_rebuild()
