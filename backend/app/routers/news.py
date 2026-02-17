import math
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlmodel import col, func, select

from app.dependencies import get_session
from app.models.analysis import AnalysisResult
from app.models.associations import NewsKeyword
from app.models.keyword import Keyword
from app.models.news import NewsArticle
from app.schemas.analysis import AnalysisResponse
from app.schemas.keyword import KeywordBrief
from app.schemas.news import (
    NewsFetchRequest,
    NewsFetchResponse,
    NewsResponse,
    PaginatedNewsResponse,
)
from app.services.news_fetcher import fetch_news_for_keywords

router = APIRouter(prefix="/api/v1/news", tags=["news"])


def _build_news_response(article: NewsArticle) -> NewsResponse:
    """Convert a NewsArticle ORM object to NewsResponse schema."""
    keywords = [
        KeywordBrief(
            id=link.keyword.id,
            keyword=link.keyword.keyword,
            category=link.keyword.category,
        )
        for link in article.keyword_links
        if link.keyword
    ]

    analysis = None
    if article.analysis:
        a = article.analysis
        analysis = AnalysisResponse(
            title_ja=a.title_ja,
            summary_ja=a.summary_ja,
            sentiment=a.sentiment,
            impact_score=a.impact_score,
            key_topics=a.key_topics,
            reasoning=a.reasoning,
            ai_provider=a.ai_provider,
            analyzed_at=a.analyzed_at,
        )

    return NewsResponse(
        id=article.id,
        title_original=article.title_original,
        url=article.url,
        source=article.source,
        published_at=article.published_at,
        fetched_at=article.fetched_at,
        keywords=keywords,
        analysis=analysis,
    )


@router.get("", response_model=PaginatedNewsResponse)
async def list_news(
    keyword_id: int | None = Query(None, alias="keywordId"),
    sentiment: str | None = None,
    min_impact: int | None = Query(None, alias="minImpact"),
    sort_by: str = Query("publishedAt", alias="sortBy"),
    sort_order: str = Query("desc", alias="sortOrder"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100, alias="perPage"),
    session: AsyncSession = Depends(get_session),
) -> PaginatedNewsResponse:
    # Base query with eager loading
    stmt = select(NewsArticle).options(
        selectinload(NewsArticle.analysis),
        selectinload(NewsArticle.keyword_links).selectinload(NewsKeyword.keyword),
    )

    # Filters
    if keyword_id is not None:
        stmt = stmt.join(NewsKeyword).where(NewsKeyword.keyword_id == keyword_id)

    if sentiment is not None:
        stmt = stmt.join(
            AnalysisResult,
            AnalysisResult.news_article_id == NewsArticle.id,
            isouter=False,
        ).where(AnalysisResult.sentiment == sentiment)

    if min_impact is not None:
        if sentiment is None:
            stmt = stmt.join(
                AnalysisResult,
                AnalysisResult.news_article_id == NewsArticle.id,
                isouter=False,
            )
        stmt = stmt.where(AnalysisResult.impact_score >= min_impact)

    # Count total before pagination
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await session.execute(count_stmt)).scalar_one()

    # Sorting
    sort_column_map = {
        "publishedAt": NewsArticle.published_at,
        "impactScore": AnalysisResult.impact_score,
    }
    sort_col = sort_column_map.get(sort_by, NewsArticle.published_at)
    if sort_by == "impactScore" and sentiment is None and min_impact is None:
        stmt = stmt.join(
            AnalysisResult,
            AnalysisResult.news_article_id == NewsArticle.id,
            isouter=True,
        )
    stmt = stmt.order_by(
        col(sort_col).desc() if sort_order == "desc" else col(sort_col).asc()
    )

    # Pagination
    offset = (page - 1) * per_page
    stmt = stmt.offset(offset).limit(per_page)

    result = await session.execute(stmt)
    articles = result.unique().scalars().all()

    return PaginatedNewsResponse(
        items=[_build_news_response(a) for a in articles],
        total=total,
        page=page,
        per_page=per_page,
        total_pages=math.ceil(total / per_page) if total > 0 else 0,
    )


@router.get("/{news_id}", response_model=NewsResponse)
async def get_news(
    news_id: int,
    session: AsyncSession = Depends(get_session),
) -> NewsResponse:
    stmt = (
        select(NewsArticle)
        .where(NewsArticle.id == news_id)
        .options(
            selectinload(NewsArticle.analysis),
            selectinload(NewsArticle.keyword_links).selectinload(
                NewsKeyword.keyword
            ),
        )
    )
    result = await session.execute(stmt)
    article = result.unique().scalar_one_or_none()

    if not article:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="News article not found",
        )

    return _build_news_response(article)


@router.post(
    "/fetch",
    response_model=NewsFetchResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def fetch_news(
    body: NewsFetchRequest | None = None,
    session: AsyncSession = Depends(get_session),
) -> NewsFetchResponse:
    # Determine target keywords
    if body and body.keyword_ids:
        stmt = select(Keyword).where(
            Keyword.id.in_(body.keyword_ids),
            Keyword.is_active == True,  # noqa: E712
        )
    else:
        stmt = select(Keyword).where(Keyword.is_active == True)  # noqa: E712

    result = await session.execute(stmt)
    keywords = list(result.scalars().all())

    fetch_result = await fetch_news_for_keywords(session, keywords)

    now = datetime.now(UTC)
    job_id = f"fetch-{now.strftime('%Y%m%d-%H%M%S')}"

    return NewsFetchResponse(
        message=(
            f"Fetch completed: {fetch_result.new_count} new,"
            f" {fetch_result.skipped_count} skipped"
        ),
        keywords_count=len(keywords),
        job_id=job_id,
    )
