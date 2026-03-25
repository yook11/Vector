from datetime import datetime
from typing import Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, func
from sqlmodel import Field, Relationship, SQLModel


class NewsArticle(SQLModel, table=True):
    __tablename__ = "news_articles"
    __table_args__ = (
        Index("idx_news_published", "published_at", postgresql_using="btree"),
        # Legacy index — kept for DB compat, dropped in Step 5
        Index("idx_news_fetched", "fetched_at", postgresql_using="btree"),
    )

    id: int | None = Field(default=None, primary_key=True)

    # --- Primary columns (new schema, used by application code) ---
    original_title: str = Field(max_length=500, nullable=False)
    original_url: str = Field(max_length=2048, nullable=False)
    original_content: str | None = Field(default=None)
    original_description: str | None = Field(default=None, max_length=2000)
    news_source_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey(
                "news_sources.id",
                ondelete="RESTRICT",
                name="fk_news_articles_news_source_id",
            ),
            nullable=False,
        )
    )
    published_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            server_default=func.now(),
            nullable=False,
        ),
    )

    # --- Legacy columns (DB NOT NULL, removed in Step 5) ---
    title_original: str = Field(max_length=500, nullable=False)
    url: str = Field(max_length=2048, nullable=False)
    source: str = Field(max_length=100, nullable=False)
    fetched_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            server_default=func.now(),
            nullable=False,
        ),
    )

    # --- Legacy columns (nullable, removed in Step 5) ---
    content: str | None = Field(default=None)
    content_fetched_at: datetime | None = Field(
        default=None, sa_type=DateTime(timezone=True)
    )
    content_fetch_attempts: int = Field(default=0, nullable=False)
    source_id: int | None = Field(
        default=None,
        sa_column=Column(
            Integer,
            ForeignKey("news_sources.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    guid: str | None = Field(default=None, max_length=2048, unique=True)
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
    embedding: list[float] | None = Field(
        default=None,
        sa_column=Column(Vector(768), nullable=True),
    )

    # --- Relationships (new) ---
    article_analysis: Optional["ArticleAnalysis"] = Relationship(
        back_populates="news_article",
        sa_relationship_kwargs={"uselist": False},
    )
    news_source: "NewsSource" = Relationship(
        back_populates="articles",
        sa_relationship_kwargs={
            "uselist": False,
            "foreign_keys": "[NewsArticle.news_source_id]",
        },
    )
    article_keywords: list["ArticleKeyword"] = Relationship(
        back_populates="news_article"
    )
    watchlist_items: list["WatchlistItem"] = Relationship(back_populates="news_article")

    # --- Legacy relationships (removed in Step 5) ---
    article_group: Optional["ArticleGroup"] = Relationship(
        back_populates="articles",
        sa_relationship_kwargs={
            "uselist": False,
            "foreign_keys": "[NewsArticle.article_group_id]",
        },
    )


# Resolve forward references
from app.models.analysis import ArticleAnalysis  # noqa: E402, F811
from app.models.article_group import ArticleGroup  # noqa: E402, F811
from app.models.associations import ArticleKeyword  # noqa: E402, F811
from app.models.news_source import NewsSource  # noqa: E402, F811
from app.models.watchlist import WatchlistItem  # noqa: E402, F811

NewsArticle.model_rebuild()
