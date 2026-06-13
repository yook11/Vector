"""Stage 4 (Assessment) で in-scope と判定された curation の評価結果 (ORM)。

translated_title / summary は curation から複製保持し、assessment 単独で
「ユーザーに見せる分析結果」として自己完結する。Category は第一級フィルタ軸
として直接 FK を持つ。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.article_curation import ArticleCuration
    from app.models.category import Category
    from app.models.watchlist_entry import WatchlistEntry


__all__ = ["AnalyzedArticleRecord"]


class AnalyzedArticleRecord(Base):
    """Stage 4 で in-scope と判定された curation の評価結果 (ORM)。"""

    __tablename__ = "analyzed_articles"
    __table_args__ = (
        UniqueConstraint("curation_id", name="uq_analyzed_articles_curation_id"),
        CheckConstraint(
            "translated_title != ''",
            name="ck_analyzed_articles_translated_title_not_empty",
        ),
        CheckConstraint(
            "summary != ''",
            name="ck_analyzed_articles_summary_not_empty",
        ),
        CheckConstraint(
            "investor_take != ''",
            name="ck_analyzed_articles_investor_take_not_empty",
        ),
        # サイドバーの直近 24 時間集計クエリ向けの複合インデックス。
        # 単独 ix_analyzed_articles_category_id は作らない（複合の左端でカバー）。
        Index(
            "ix_analyzed_articles_category_id_analyzed_at",
            "category_id",
            "analyzed_at",
        ),
        Index(
            "idx_analyzed_articles_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "halfvec_cosine_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    curation_id: Mapped[int] = mapped_column(
        ForeignKey("article_curations.id", ondelete="CASCADE"),
    )
    translated_title: Mapped[str] = mapped_column(String(500))
    summary: Mapped[str] = mapped_column(Text())
    investor_take: Mapped[str] = mapped_column(Text())
    analyzed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    embedding: Mapped[list[float] | None] = mapped_column(HALFVEC(768))
    # 第一級フィルタ軸。
    category_id: Mapped[int] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT"),
    )
    # 記事の重要な情報と登場固有名のペア配列。NULL = 旧行、[] = AI が
    # key_points を返さなかった行、values = AI 抽出済み行。
    key_points: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB, nullable=True
    )

    # リレーション
    curation: Mapped[ArticleCuration] = relationship(back_populates="analyzed_article")
    watchlist_entries: Mapped[list[WatchlistEntry]] = relationship(
        back_populates="analyzed_article"
    )
    # category_id FK 経由の直 relationship（カード表示用）。逆向き
    # Category.analyzed_articles は持たない（逆引きは category_id filter で足りる）。
    category: Mapped[Category] = relationship()
