from datetime import UTC, datetime
from typing import Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer
from sqlmodel import Field, Relationship, SQLModel


class NewsArticle(SQLModel, table=True):
    __tablename__ = "news_articles"
    __table_args__ = (
        Index("idx_news_published", "published_at", postgresql_using="btree"),
        Index("idx_news_fetched", "fetched_at", postgresql_using="btree"),
    )

    id: int | None = Field(default=None, primary_key=True)
    title_original: str = Field(max_length=500, nullable=False)
    description_original: str | None = Field(default=None)
    url: str = Field(max_length=2048, unique=True, nullable=False, index=True)
    source: str = Field(max_length=100, nullable=False)
    published_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    fetched_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        nullable=False,
        sa_type=DateTime(timezone=True),
    )
    content: str | None = Field(default=None)
    content_fetched_at: datetime | None = Field(
        default=None, sa_type=DateTime(timezone=True)
    )
    content_fetch_attempts: int = Field(default=0, nullable=False)
    embedding: list[float] | None = Field(
        default=None,
        sa_column=Column(Vector(768), nullable=True),
    )

    # A-2: source tracking and deduplication
    source_id: int | None = Field(
        default=None,
        sa_column=Column(
            Integer,
            ForeignKey("news_sources.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    guid: str | None = Field(default=None, max_length=2048, unique=True)

    # 3B-1: duplicate article grouping
    article_group_id: int | None = Field(
        default=None,
        sa_column=Column(
            Integer,
            ForeignKey(
                "article_groups.id",
                ondelete="SET NULL",
                name="fk_news_articles_article_group_id",
                use_alter=True,
            ),
            nullable=True,
            index=True,
        ),
    )

    # Relationships
    analyses: list["AnalysisResult"] = Relationship(back_populates="news_article")
    article_keywords: list["ArticleKeyword"] = Relationship(
        back_populates="news_article"
    )
    watchlist_items: list["WatchlistItem"] = Relationship(back_populates="news_article")
    source_ref: "NewsSource" = Relationship(
        back_populates="articles",
        sa_relationship_kwargs={"uselist": False},
    )
    article_group: Optional["ArticleGroup"] = Relationship(
        back_populates="articles",
        sa_relationship_kwargs={
            "uselist": False,
            "foreign_keys": "[NewsArticle.article_group_id]",
        },
    )


# Resolve forward references
from app.models.analysis import AnalysisResult  # noqa: E402, F811
from app.models.article_group import ArticleGroup  # noqa: E402, F811
from app.models.associations import ArticleKeyword  # noqa: E402, F811
from app.models.news_source import NewsSource  # noqa: E402, F811
from app.models.watchlist import WatchlistItem  # noqa: E402, F811

NewsArticle.model_rebuild()
