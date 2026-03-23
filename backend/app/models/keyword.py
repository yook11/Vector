from datetime import UTC, datetime

from sqlalchemy import DateTime
from sqlmodel import Field, Relationship, SQLModel


class Keyword(SQLModel, table=True):
    __tablename__ = "keywords"

    id: int | None = Field(default=None, primary_key=True)
    keyword: str = Field(max_length=200, unique=True, nullable=False)
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
    news_links: list["NewsKeyword"] = Relationship(back_populates="keyword")
    user_subscriptions: list["UserKeywordSubscription"] = Relationship(
        back_populates="keyword"
    )
    category_links: list["KeywordCategoryLink"] = Relationship(back_populates="keyword")


# Resolve forward references
from app.models.associations import NewsKeyword  # noqa: E402, F811
from app.models.category import KeywordCategoryLink  # noqa: E402, F811
from app.models.user_keyword import UserKeywordSubscription  # noqa: E402, F811

Keyword.model_rebuild()
