from datetime import UTC, datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer
from sqlmodel import Field, Relationship, SQLModel


class Keyword(SQLModel, table=True):
    __tablename__ = "keywords"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(max_length=100, unique=True, nullable=False)
    category_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("categories.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        )
    )
    status: str = Field(default="provisional", max_length=20, nullable=False)
    is_ai_generated: bool = Field(default=False, nullable=False)
    approved_at: datetime | None = Field(
        default=None,
        nullable=True,
        sa_type=DateTime(timezone=True),
    )
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
    category: "Category" = Relationship(back_populates="keywords")
    article_keywords: list["ArticleKeyword"] = Relationship(back_populates="keyword")


# Resolve forward references
from app.models.associations import ArticleKeyword  # noqa: E402, F811
from app.models.category import Category  # noqa: E402, F811

Keyword.model_rebuild()
