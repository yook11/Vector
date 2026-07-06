"""Internal article vector search boundary."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.internal_retrieval.query_embedding import InternalQueryEmbedding
from app.analysis.analyzed_article import InScopeAnalyzedArticle
from app.models.analyzable_article_record import AnalyzableArticleRecord
from app.models.analyzed_article_record import AnalyzedArticleRecord
from app.models.article_curation import ArticleCuration
from app.models.category import Category

__all__ = [
    "InternalArticleContent",
    "InternalArticleSearchHit",
    "PgVectorArticleSearchRepository",
]


class InternalArticleContent(BaseModel):
    """Answer-generation projection of an in-scope analyzed article."""

    model_config = ConfigDict(frozen=True)

    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    key_points: list[str] = Field(default_factory=list)
    mentions: list[str] = Field(default_factory=list)
    published_at: datetime | None = None

    @classmethod
    def from_article(
        cls,
        article: InScopeAnalyzedArticle,
        *,
        published_at: datetime | None,
    ) -> InternalArticleContent:
        mention_surfaces: list[str] = []
        seen_mentions: set[str] = set()
        for key_point in article.assessment_result.key_points:
            for mention in key_point.mentions:
                key = mention.surface.casefold()
                if key in seen_mentions:
                    continue
                seen_mentions.add(key)
                mention_surfaces.append(mention.surface)

        return cls(
            title=article.title,
            summary=article.summary,
            key_points=[
                key_point.content for key_point in article.assessment_result.key_points
            ],
            mentions=mention_surfaces,
            published_at=published_at,
        )


class InternalArticleSearchHit(BaseModel):
    """Internal vector search hit with the public /news article id."""

    model_config = ConfigDict(frozen=True)

    assessment_id: int = Field(gt=0)
    article: InScopeAnalyzedArticle
    content: InternalArticleContent
    distance: float = Field(ge=0)


class PgVectorArticleSearchRepository:
    """Search in-scope analyzed articles by query embedding."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def search_by_embedding(
        self,
        embedding: InternalQueryEmbedding,
        *,
        limit: int,
    ) -> list[InternalArticleSearchHit]:
        if limit <= 0:
            return []

        query_vector = embedding.vector.to_list()
        distance = AnalyzedArticleRecord.embedding.cosine_distance(query_vector).label(
            "distance"
        )
        stmt = (
            select(
                AnalyzedArticleRecord.id.label("assessment_id"),
                AnalyzedArticleRecord.curation_id,
                AnalyzedArticleRecord.translated_title,
                AnalyzedArticleRecord.summary,
                AnalyzedArticleRecord.investor_take,
                AnalyzedArticleRecord.key_points,
                Category.slug.label("category_slug"),
                AnalyzableArticleRecord.published_at,
                distance,
            )
            .select_from(AnalyzedArticleRecord)
            .join(
                ArticleCuration,
                ArticleCuration.id == AnalyzedArticleRecord.curation_id,
            )
            .join(
                AnalyzableArticleRecord,
                AnalyzableArticleRecord.id == ArticleCuration.analyzable_article_id,
            )
            .join(Category, Category.id == AnalyzedArticleRecord.category_id)
            .where(AnalyzedArticleRecord.embedding.is_not(None))
            .order_by(
                distance.asc(),
                AnalyzableArticleRecord.published_at.desc().nulls_last(),
            )
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).all()

        hits: list[InternalArticleSearchHit] = []
        for row in rows:
            hit = _hit_from_row(row)
            if hit is not None:
                hits.append(hit)
        return hits


def _hit_from_row(row: Any) -> InternalArticleSearchHit | None:
    try:
        article = InScopeAnalyzedArticle.from_persisted_values(
            curation_id=row.curation_id,
            translated_title=row.translated_title,
            summary=row.summary,
            category_slug=str(row.category_slug),
            investor_take=row.investor_take,
            key_points=row.key_points,
        )
    except ValidationError:
        return None

    return InternalArticleSearchHit(
        assessment_id=row.assessment_id,
        article=article,
        content=InternalArticleContent.from_article(
            article,
            published_at=row.published_at,
        ),
        distance=float(row.distance),
    )
