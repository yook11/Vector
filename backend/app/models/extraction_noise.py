"""Stage 1 (Gemini extraction) で noise 判定された記事の永続化 ORM モデル。

``article_extractions`` (signal 側) と排他関係を DB トリガー対称ペア
(``p1_add_extraction_noises`` migration) で構造的に強制する。1 article に
対し ``article_extractions`` または ``extraction_noises`` のどちらか一方
しか存在できない。

``entities`` は JSONB カラムとして同テーブル内に保持する。noise 記事の
entities は遡及検証 (プロンプト改訂時の ad-hoc 分析) 専用で個別 entity
単位の JOIN/WHERE は想定しないため、子テーブル分離は採らない。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.article import Article

__all__ = ["ExtractionNoise"]


class ExtractionNoise(Base):
    """Stage 1 で ``relevance="noise"`` と判定された記事の記録。"""

    __tablename__ = "extraction_noises"
    __table_args__ = (
        UniqueConstraint("article_id", name="uq_extraction_noises_article_id"),
        CheckConstraint(
            "title_ja <> ''",
            name="ck_extraction_noises_title_ja_not_empty",
        ),
        CheckConstraint(
            "summary_ja <> ''",
            name="ck_extraction_noises_summary_ja_not_empty",
        ),
        CheckConstraint(
            "ai_model <> ''",
            name="ck_extraction_noises_ai_model_not_empty",
        ),
        CheckConstraint(
            "jsonb_typeof(entities) = 'array'",
            name="ck_extraction_noises_entities_is_array",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    article_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("articles.id", ondelete="CASCADE"),
    )
    title_ja: Mapped[str] = mapped_column(String(500))
    summary_ja: Mapped[str] = mapped_column(Text())
    entities: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    ai_model: Mapped[str] = mapped_column(String(100))
    rejected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    article: Mapped[Article] = relationship(back_populates="extraction_noise")
