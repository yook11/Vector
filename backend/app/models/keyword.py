from datetime import UTC, datetime

from sqlalchemy import DateTime
from sqlmodel import Field, Relationship, SQLModel


class Keyword(SQLModel, table=True):
    __tablename__ = "keywords"

    id: int | None = Field(default=None, primary_key=True)
    keyword: str = Field(max_length=200, unique=True, nullable=False)
    category: str = Field(max_length=50, default="custom", nullable=False)
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
    news_links: list["NewsKeyword"] = Relationship(back_populates="keyword")


# Resolve forward reference
from app.models.associations import NewsKeyword  # noqa: E402, F811

Keyword.model_rebuild()
