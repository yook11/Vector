from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlmodel import Field, Relationship, SQLModel


class KeywordStatus(StrEnum):
    PROVISIONAL = "provisional"
    OFFICIAL = "official"
    BLACKLISTED = "blacklisted"


class Keyword(SQLModel, table=True):
    __tablename__ = "keywords"
    __table_args__ = (
        CheckConstraint(
            "(status = 'official' AND approved_at IS NOT NULL) "
            "OR (status != 'official' AND approved_at IS NULL)",
            name="ck_keywords_status_approved_at",
        ),
    )

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
    status: KeywordStatus = Field(
        default=KeywordStatus.PROVISIONAL, sa_type=String(20), nullable=False
    )
    is_ai_generated: bool = Field(default=False, nullable=False)
    approved_at: datetime | None = Field(
        default=None,
        nullable=True,
        sa_type=DateTime(timezone=True),
    )
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            server_default=func.now(),
            nullable=False,
        ),
    )
    updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            server_default=func.now(),
            nullable=False,
        ),
    )

    # Relationships
    category: "Category" = Relationship(back_populates="keywords")
    article_keywords: list["ArticleKeyword"] = Relationship(back_populates="keyword")


# Resolve forward references
from app.models.associations import ArticleKeyword  # noqa: E402, F811
from app.models.category import Category  # noqa: E402, F811

Keyword.model_rebuild()
