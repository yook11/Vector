import math

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import case
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlmodel import func, select

from app.config import settings
from app.dependencies import CurrentUser, get_admin_user, get_optional_user, get_session
from app.models.article_analysis import ArticleAnalysis, ImpactLevel
from app.models.article_keyword import ArticleKeyword
from app.models.keyword import Keyword
from app.models.news_article import NewsArticle
from app.models.watchlist_entry import WatchlistEntry
from app.schemas.embeds import KeywordEmbed, NewsSourceEmbed, OriginalArticleEmbed
from app.schemas.news import (
    EmbedResponse,
    NewsBrief,
    NewsDetail,
    NewsFetchRequest,
    NewsFetchResponse,
    PaginatedNewsResponse,
)
from app.services.embedding import EmbeddingError, embed_articles, embed_search_query
from app.tasks.pipeline_tasks import fetch_metadata

router = APIRouter(prefix="/api/v1/news", tags=["news"])

# Impact level ordering for sort/filter (CASE expression)
_impact_order_expr = case(
    (ArticleAnalysis.impact_level == ImpactLevel.LOW, 1),
    (ArticleAnalysis.impact_level == ImpactLevel.MEDIUM, 2),
    (ArticleAnalysis.impact_level == ImpactLevel.HIGH, 3),
    (ArticleAnalysis.impact_level == ImpactLevel.CRITICAL, 4),
    else_=0,
)

_IMPACT_LEVEL_ORDER = {
    ImpactLevel.LOW: 1,
    ImpactLevel.MEDIUM: 2,
    ImpactLevel.HIGH: 3,
    ImpactLevel.CRITICAL: 4,
}


async def _get_watched_ids(session: AsyncSession, user: CurrentUser | None) -> set[int]:
    """Return set of news_article_ids in the user's watchlist."""
    if user is None:
        return set()
    stmt = select(WatchlistEntry.news_article_id).where(
        WatchlistEntry.user_id == user.id
    )
    result = await session.execute(stmt)
    return set(result.scalars().all())


def _build_keyword_embeds(article: NewsArticle) -> list[KeywordEmbed]:
    """Extract keyword embeds from a NewsArticle ORM object."""
    return [
        KeywordEmbed(
            id=link.keyword.id,
            name=link.keyword.name,
        )
        for link in article.article_keywords
        if link.keyword
    ]


def _build_news_brief(
    article: NewsArticle,
    watched_ids: set[int] | None = None,
) -> NewsBrief:
    """Convert a NewsArticle ORM object to NewsBrief schema.

    Requires article.article_analysis to be present (INNER JOIN).
    """
    a = article.article_analysis
    return NewsBrief(
        id=article.id,
        translated_title=a.translated_title,
        summary=a.summary,
        impact_level=a.impact_level,
        source=NewsSourceEmbed(
            id=article.news_source.id,
            name=article.news_source.name,
        ),
        published_at=article.published_at,
        keywords=_build_keyword_embeds(article),
        is_watched=article.id in watched_ids if watched_ids else False,
    )


def _build_news_detail(
    article: NewsArticle,
    watched_ids: set[int] | None = None,
) -> NewsDetail:
    """Convert a NewsArticle ORM object to NewsDetail schema.

    Requires article.article_analysis to be present.
    """
    a = article.article_analysis
    return NewsDetail(
        id=article.id,
        translated_title=a.translated_title,
        summary=a.summary,
        impact_level=a.impact_level,
        reasoning=a.reasoning,
        analyzed_at=a.analyzed_at,
        source=NewsSourceEmbed(
            id=article.news_source.id,
            name=article.news_source.name,
        ),
        published_at=article.published_at,
        keywords=_build_keyword_embeds(article),
        is_watched=article.id in watched_ids if watched_ids else False,
        original=OriginalArticleEmbed(
            title=article.original_title,
            url=article.original_url,
            content=article.original_content,
        ),
    )


def _news_eager_options() -> list:
    """Return common selectinload options for news queries."""
    return [
        selectinload(NewsArticle.article_analysis),
        selectinload(NewsArticle.news_source),
        selectinload(NewsArticle.article_keywords)
        .selectinload(ArticleKeyword.keyword)
        .selectinload(Keyword.category),
    ]


@router.get("", response_model=PaginatedNewsResponse)
async def list_news(
    keyword_id: int | None = Query(None, alias="keywordId"),
    kw_category_id: int | None = Query(None, alias="kwCategoryId"),
    source_id: int | None = Query(None, alias="sourceId"),
    impact_level: ImpactLevel | None = Query(None, alias="impactLevel"),
    q: str | None = Query(None, min_length=1, max_length=500),
    sort_by: str = Query("publishedAt", alias="sortBy"),
    sort_order: str = Query("desc", alias="sortOrder"),
    page: int = Query(1, ge=1),
    per_page: int = Query(12, ge=1, le=100, alias="perPage"),
    user: CurrentUser | None = Depends(get_optional_user),
    session: AsyncSession = Depends(get_session),
) -> PaginatedNewsResponse:
    # Base query: INNER JOIN ArticleAnalysis to return only analyzed articles
    stmt = (
        select(NewsArticle)
        .join(ArticleAnalysis, ArticleAnalysis.news_article_id == NewsArticle.id)
        .options(*_news_eager_options())
    )

    # Semantic search: embed query and filter by cosine distance
    query_embedding: list[float] | None = None

    if q is not None:
        try:
            query_embedding = await embed_search_query(q)
        except EmbeddingError:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Search embedding generation failed. Please try again.",
            )
        stmt = stmt.where(ArticleAnalysis.embedding.is_not(None))
        distance_expr = ArticleAnalysis.embedding.cosine_distance(query_embedding)
        stmt = stmt.where(distance_expr < settings.semantic_search_max_distance)

    if source_id is not None:
        stmt = stmt.where(NewsArticle.news_source_id == source_id)

    if keyword_id is not None:
        matching_ids = select(ArticleKeyword.news_article_id).where(
            ArticleKeyword.keyword_id == keyword_id
        )
        stmt = stmt.where(NewsArticle.id.in_(matching_ids))
    elif kw_category_id is not None:
        sub_kw_ids = select(Keyword.id).where(Keyword.category_id == kw_category_id)
        matching_ids = select(ArticleKeyword.news_article_id).where(
            ArticleKeyword.keyword_id.in_(sub_kw_ids)
        )
        stmt = stmt.where(NewsArticle.id.in_(matching_ids))

    if impact_level is not None:
        min_order = _IMPACT_LEVEL_ORDER[impact_level]
        stmt = stmt.where(_impact_order_expr >= min_order)

    # Count total before pagination
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await session.execute(count_stmt)).scalar_one()

    # Sorting
    is_default_sort = sort_by == "publishedAt" and sort_order == "desc"
    if query_embedding is not None and is_default_sort:
        distance_expr = ArticleAnalysis.embedding.cosine_distance(query_embedding)
        stmt = stmt.order_by(distance_expr.asc())
    else:
        if sort_by == "impactLevel":
            order_expr = _impact_order_expr
        else:
            order_expr = NewsArticle.published_at
        stmt = stmt.order_by(
            order_expr.desc() if sort_order == "desc" else order_expr.asc()
        )

    # Pagination
    offset = (page - 1) * per_page
    stmt = stmt.offset(offset).limit(per_page)

    result = await session.execute(stmt)
    articles = result.unique().scalars().all()

    watched_ids = await _get_watched_ids(session, user)

    return PaginatedNewsResponse(
        items=[_build_news_brief(a, watched_ids) for a in articles],
        total=total,
        page=page,
        per_page=per_page,
        total_pages=math.ceil(total / per_page) if total > 0 else 0,
    )


@router.post(
    "/embed",
    response_model=EmbedResponse,
    status_code=status.HTTP_200_OK,
    summary="Backfill embeddings for analyses that are missing them",
)
async def embed_news(
    session: AsyncSession = Depends(get_session),
    _user: CurrentUser = Depends(get_admin_user),
) -> EmbedResponse:
    """Generate vector embeddings for all analyses where embedding IS NULL.

    Requires authentication to prevent unintended Gemini API cost.
    """
    stmt = select(ArticleAnalysis).where(ArticleAnalysis.embedding.is_(None))
    result = await session.execute(stmt)
    analyses = list(result.scalars().all())

    if not analyses:
        return EmbedResponse(
            message="No analyses need embedding",
            embedded_count=0,
            skipped_count=0,
            error_count=0,
        )

    er = await embed_articles(session, analyses)

    return EmbedResponse(
        message=f"Embedding completed: {er.embedded_count} embedded, "
        f"{er.error_count} errors",
        embedded_count=er.embedded_count,
        skipped_count=er.skipped_count,
        error_count=er.error_count,
    )


@router.get(
    "/{news_id}/similar",
    response_model=list[NewsBrief],
    summary="Find semantically similar articles using pgvector cosine distance",
)
async def get_similar_news(
    news_id: int,
    limit: int = Query(5, ge=1, le=20),
    session: AsyncSession = Depends(get_session),
) -> list[NewsBrief]:
    """Return articles most similar to the given article, ordered by cosine distance.

    Returns an empty list (not 404) if the article has no embedding yet.
    """
    # Get the source article's analysis embedding
    source_analysis = (
        await session.execute(
            select(ArticleAnalysis).where(ArticleAnalysis.news_article_id == news_id)
        )
    ).scalar_one_or_none()

    if source_analysis is None or source_analysis.embedding is None:
        # Check if article exists at all
        article_exists = (
            await session.execute(
                select(NewsArticle.id).where(NewsArticle.id == news_id)
            )
        ).scalar_one_or_none()
        if article_exists is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="News article not found",
            )
        return []

    similar_stmt = (
        select(NewsArticle)
        .join(ArticleAnalysis, ArticleAnalysis.news_article_id == NewsArticle.id)
        .options(*_news_eager_options())
        .where(
            NewsArticle.id != news_id,
            ArticleAnalysis.embedding.is_not(None),
        )
        .order_by(ArticleAnalysis.embedding.cosine_distance(source_analysis.embedding))
        .limit(limit)
    )

    similar_result = await session.execute(similar_stmt)
    articles = similar_result.unique().scalars().all()

    return [_build_news_brief(a) for a in articles]


@router.get("/{news_id}", response_model=NewsDetail)
async def get_news(
    news_id: int,
    user: CurrentUser | None = Depends(get_optional_user),
    session: AsyncSession = Depends(get_session),
) -> NewsDetail:
    stmt = (
        select(NewsArticle)
        .where(NewsArticle.id == news_id)
        .options(*_news_eager_options())
    )
    result = await session.execute(stmt)
    article = result.unique().scalar_one_or_none()

    if not article or article.article_analysis is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="News article not found",
        )

    watched_ids = await _get_watched_ids(session, user)
    return _build_news_detail(article, watched_ids)


@router.post(
    "/fetch",
    response_model=NewsFetchResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def fetch_news(
    body: NewsFetchRequest | None = None,
    _user: CurrentUser = Depends(get_admin_user),
) -> NewsFetchResponse:
    """Enqueue a news fetch task. Returns immediately with a task ID."""
    source_ids = body.source_ids if body else None
    task = await fetch_metadata.kiq(source_ids=source_ids)

    return NewsFetchResponse(
        message="Fetch task submitted",
        sources_count=len(source_ids) if source_ids else None,
        job_id=task.task_id,
    )
