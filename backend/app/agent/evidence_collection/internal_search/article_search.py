"""Internal article vector search boundary."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.evidence_collection.internal_search.contract import (
    InternalArticleContent,
    InternalArticleSearchHit,
    InternalSearchError,
)
from app.agent.evidence_collection.internal_search.metrics import (
    InternalHitDropReason,
    record_internal_hit_dropped,
)
from app.agent.evidence_collection.internal_search.query_embedding import (
    InternalQueryEmbedding,
)
from app.analysis.analyzed_article import InScopeAnalyzedArticle
from app.models.analyzable_article_record import AnalyzableArticleRecord
from app.models.analyzed_article_record import AnalyzedArticleRecord
from app.models.article_curation import ArticleCuration
from app.models.category import Category

__all__ = [
    "PgVectorArticleSearchRepository",
]


class PgVectorArticleSearchRepository:
    """Search in-scope analyzed articles by query embedding."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

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
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    rows = (await session.execute(stmt)).all()
        except (SQLAlchemyTimeoutError, OperationalError) as exc:
            raise InternalSearchError(phase="article_search") from exc
        except InterfaceError as exc:
            if not exc.connection_invalidated:
                raise
            raise InternalSearchError(phase="article_search") from exc

        hits: list[InternalArticleSearchHit] = []
        for row in rows:
            hit = _hit_from_search_row(row)
            if hit is not None:
                hits.append(hit)
        return hits


def _hit_from_search_row(row: Any) -> InternalArticleSearchHit | None:
    try:
        article = InScopeAnalyzedArticle.from_persisted_values(
            curation_id=row.curation_id,
            translated_title=row.translated_title,
            summary=row.summary,
            category_slug=str(row.category_slug),
            investor_take=row.investor_take,
            key_points=row.key_points,
        )
    except ValidationError as exc:
        record_internal_hit_dropped(reason=_drop_reason(exc))
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


def _drop_reason(exc: ValidationError) -> InternalHitDropReason:
    """summaryの長さ超過だけを切り分け、他はrow_invalidへ畳む。"""
    for error in exc.errors():
        if error["type"] == "string_too_long" and error["loc"] == ("summary",):
            return "summary_too_long"
    return "row_invalid"
