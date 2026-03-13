from datetime import UTC, datetime
from typing import Optional

from sqlalchemy import Column as SAColumn
from sqlalchemy import DateTime, ForeignKey, Index, Integer
from sqlmodel import Field, Relationship, SQLModel


class ArticleGroup(SQLModel, table=True):
    __tablename__ = "article_groups"
    __table_args__ = (
        Index(
            "idx_article_groups_canonical",
            "canonical_id",
            postgresql_using="btree",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    canonical_id: int | None = Field(
        default=None,
        sa_column=SAColumn(
            Integer,
            ForeignKey(
                "news_articles.id",
                ondelete="SET NULL",
                name="fk_article_groups_canonical_id",
                use_alter=True,
            ),
            nullable=True,
        ),
    )
    article_count: int = Field(default=1, nullable=False)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        nullable=False,
        sa_type=DateTime(timezone=True),
    )

    # Relationships
    canonical_article: Optional["NewsArticle"] = Relationship(
        sa_relationship_kwargs={
            "uselist": False,
            "foreign_keys": "[ArticleGroup.canonical_id]",
        },
    )
    articles: list["NewsArticle"] = Relationship(
        back_populates="article_group",
        sa_relationship_kwargs={
            "foreign_keys": "[NewsArticle.article_group_id]",
        },
    )


# Resolve forward references
from app.models.news import NewsArticle  # noqa: E402, F811

ArticleGroup.model_rebuild()
