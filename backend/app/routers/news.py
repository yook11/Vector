import math

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlmodel import col, func, select

from app.config import settings
from app.dependencies import get_current_user, get_optional_user, get_session
from app.models.analysis import AnalysisResult
from app.models.associations import NewsKeyword
from app.models.investment_category import (
    AnalysisInvestmentCategory,
    InvestmentCategory,
)
from app.models.keyword import Keyword
from app.models.keyword_category import (
    KeywordCategory,
    KeywordCategoryLink,
)
from app.models.news import NewsArticle
from app.models.user import User
from app.models.user_keyword import UserKeywordSubscription
from app.models.watchlist import WatchlistItem
from app.schemas.analysis import AIModelBrief, AnalysisResponse
from app.schemas.category import CategoryBrief
from app.schemas.keyword import KeywordBrief
from app.schemas.keyword_category import KeywordCategoryBrief
from app.schemas.news import (
    EmbedResponse,
    NewsFetchRequest,
    NewsFetchResponse,
    NewsResponse,
    PaginatedNewsResponse,
)
from app.services.embedding import embed_articles
from app.tasks.taskiq_worker import fetch_and_analyze_task

router = APIRouter(prefix="/api/v1/news", tags=["news"])

DEFAULT_LOCALE = "ja"


def _get_translated(translations: list, locale: str, field: str = "name") -> str:
    """Get a translated field value from a list of translation objects."""
    for t in translations:
        if t.locale == locale:
            return getattr(t, field, "")
    return ""


def _get_default_analysis(article: NewsArticle) -> AnalysisResult | None:
    """Return the default model's analysis (filtered eager load ensures at most 1)."""
    return article.analyses[0] if article.analyses else None


async def _get_watched_ids(session: AsyncSession, user: User | None) -> set[int]:
    """Return set of news_article_ids in the user's watchlist."""
    if user is None:
        return set()
    stmt = select(WatchlistItem.news_article_id).where(WatchlistItem.user_id == user.id)
    result = await session.execute(stmt)
    return set(result.scalars().all())


def _build_news_response(
    article: NewsArticle,
    watched_ids: set[int] | None = None,
    locale: str = DEFAULT_LOCALE,
) -> NewsResponse:
    """Convert a NewsArticle ORM object to NewsResponse schema."""
    keywords = [
        KeywordBrief(
            id=link.keyword.id,
            keyword=link.keyword.keyword,
            categories=[
                KeywordCategoryBrief(
                    slug=cl.category.slug,
                    name=_get_translated(cl.category.translations, locale),
                )
                for cl in link.keyword.category_links
                if cl.category
            ],
        )
        for link in article.keyword_links
        if link.keyword
    ]

    analysis = None
    a = _get_default_analysis(article)
    if a is not None:
        categories = [
            CategoryBrief(
                slug=link.category.slug,
                name=_get_translated(link.category.translations, locale),
            )
            for link in a.category_links
            if link.category
        ]
        analysis = AnalysisResponse(
            title=_get_translated(a.translations, locale, "title"),
            summary=_get_translated(a.translations, locale, "summary"),
            sentiment=a.sentiment,
            impact_score=a.impact_score,
            reasoning=a.reasoning,
            ai_model=AIModelBrief(
                id=a.ai_model.id,
                provider=a.ai_model.provider,
                name=a.ai_model.name,
            ),
            analyzed_at=a.analyzed_at,
            investment_categories=categories,
        )

    return NewsResponse(
        id=article.id,
        title_original=article.title_original,
        url=article.url,
        source=article.source,
        published_at=article.published_at,
        fetched_at=article.fetched_at,
        content=article.content,
        content_fetched_at=article.content_fetched_at,
        keywords=keywords,
        analysis=analysis,
        is_watched=article.id in watched_ids if watched_ids else False,
    )


def _analyses_filtered_load():
    """Filtered eager load: only the default AI model's analysis."""
    return selectinload(
        NewsArticle.analyses.and_(
            AnalysisResult.ai_model_id == settings.default_ai_model_id
        )
    )


def _news_eager_options() -> list:
    """Return common selectinload options for news queries."""
    return [
        _analyses_filtered_load()
        .selectinload(AnalysisResult.category_links)
        .selectinload(AnalysisInvestmentCategory.category)
        .selectinload(InvestmentCategory.translations),
        _analyses_filtered_load().selectinload(AnalysisResult.translations),
        _analyses_filtered_load().selectinload(AnalysisResult.ai_model),
        selectinload(NewsArticle.keyword_links)
        .selectinload(NewsKeyword.keyword)
        .selectinload(Keyword.category_links)
        .selectinload(KeywordCategoryLink.category)
        .selectinload(KeywordCategory.translations),
    ]


@router.get("", response_model=PaginatedNewsResponse)
async def list_news(
    keyword_id: int | None = Query(None, alias="keywordId"),
    kw_category_id: int | None = Query(None, alias="kwCategoryId"),
    source_id: int | None = Query(None, alias="sourceId"),
    my_keywords: bool = Query(False, alias="myKeywords"),
    sentiment: str | None = None,
    min_impact: int | None = Query(None, alias="minImpact"),
    category: str | None = None,
    sort_by: str = Query("publishedAt", alias="sortBy"),
    sort_order: str = Query("desc", alias="sortOrder"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100, alias="perPage"),
    locale: str = Query(DEFAULT_LOCALE),
    user: User | None = Depends(get_optional_user),
    session: AsyncSession = Depends(get_session),
) -> PaginatedNewsResponse:
    # Base query with eager loading (including category + translation chains)
    stmt = select(NewsArticle).options(*_news_eager_options())

    if source_id is not None:
        stmt = stmt.where(NewsArticle.source_id == source_id)

    # myKeywords filter: only effective for authenticated users
    if my_keywords and user is not None:
        sub_kw_ids = select(UserKeywordSubscription.keyword_id).where(
            UserKeywordSubscription.user_id == user.id
        )
        stmt = stmt.join(NewsKeyword).where(NewsKeyword.keyword_id.in_(sub_kw_ids))
    elif keyword_id is not None:
        # keywordId is the most specific filter — when both kwCategoryId and
        # keywordId are provided, keywordId alone is sufficient because the
        # keyword already belongs to that category. kwCategoryId stays in the
        # URL only for frontend sidebar active-state rendering.
        stmt = stmt.join(NewsKeyword).where(NewsKeyword.keyword_id == keyword_id)
    elif kw_category_id is not None:
        # Filter by all keywords belonging to this keyword category
        sub_kw_ids = select(KeywordCategoryLink.keyword_id).where(
            KeywordCategoryLink.category_id == kw_category_id
        )
        stmt = stmt.join(NewsKeyword).where(NewsKeyword.keyword_id.in_(sub_kw_ids))

    # Track whether AnalysisResult is already joined
    analysis_joined = False

    # Common JOIN condition: article + default model filter
    _analysis_join_cond = (AnalysisResult.news_article_id == NewsArticle.id) & (
        AnalysisResult.ai_model_id == settings.default_ai_model_id
    )

    if sentiment is not None:
        stmt = stmt.join(
            AnalysisResult,
            _analysis_join_cond,
            isouter=False,
        ).where(AnalysisResult.sentiment == sentiment)
        analysis_joined = True

    if min_impact is not None:
        if not analysis_joined:
            stmt = stmt.join(
                AnalysisResult,
                _analysis_join_cond,
                isouter=False,
            )
            analysis_joined = True
        stmt = stmt.where(AnalysisResult.impact_score >= min_impact)

    if category is not None:
        if not analysis_joined:
            stmt = stmt.join(
                AnalysisResult,
                _analysis_join_cond,
                isouter=False,
            )
            analysis_joined = True
        stmt = (
            stmt.join(
                AnalysisInvestmentCategory,
                AnalysisInvestmentCategory.analysis_id == AnalysisResult.id,
            )
            .join(
                InvestmentCategory,
                InvestmentCategory.id == AnalysisInvestmentCategory.category_id,
            )
            .where(InvestmentCategory.slug == category)
        )

    # Count total before pagination
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await session.execute(count_stmt)).scalar_one()

    # Sorting
    sort_column_map = {
        "publishedAt": NewsArticle.published_at,
        "impactScore": AnalysisResult.impact_score,
    }
    sort_col = sort_column_map.get(sort_by, NewsArticle.published_at)
    if sort_by == "impactScore" and not analysis_joined:
        stmt = stmt.join(
            AnalysisResult,
            _analysis_join_cond,
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

    # Compute watched IDs for the user
    watched_ids = await _get_watched_ids(session, user)

    return PaginatedNewsResponse(
        items=[_build_news_response(a, watched_ids, locale) for a in articles],
        total=total,
        page=page,
        per_page=per_page,
        total_pages=math.ceil(total / per_page) if total > 0 else 0,
    )


@router.post(
    "/embed",
    response_model=EmbedResponse,
    status_code=status.HTTP_200_OK,
    summary="Backfill embeddings for articles that are missing them",
)
async def embed_news(
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(get_current_user),
) -> EmbedResponse:
    """Generate vector embeddings for all articles where embedding IS NULL.

    Requires authentication to prevent unintended Gemini API cost.
    """
    stmt = select(NewsArticle).where(NewsArticle.embedding.is_(None))
    result = await session.execute(stmt)
    articles = list(result.scalars().all())

    if not articles:
        return EmbedResponse(
            message="No articles need embedding",
            embedded_count=0,
            skipped_count=0,
            error_count=0,
        )

    er = await embed_articles(session, articles)

    return EmbedResponse(
        message=f"Embedding completed: {er.embedded_count} embedded, "
        f"{er.error_count} errors",
        embedded_count=er.embedded_count,
        skipped_count=er.skipped_count,
        error_count=er.error_count,
    )


@router.get(
    "/{news_id}/similar",
    response_model=list[NewsResponse],
    summary="Find semantically similar articles using pgvector cosine distance",
)
async def get_similar_news(
    news_id: int,
    limit: int = Query(5, ge=1, le=20),
    locale: str = Query(DEFAULT_LOCALE),
    session: AsyncSession = Depends(get_session),
) -> list[NewsResponse]:
    """Return articles most similar to the given article, ordered by cosine distance.

    Returns an empty list (not 404) if the article has no embedding yet.
    """
    # Fetch the source article
    source_stmt = select(NewsArticle).where(NewsArticle.id == news_id)
    source_result = await session.execute(source_stmt)
    source = source_result.scalar_one_or_none()

    if source is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="News article not found",
        )

    # Graceful fallback: embedding not yet generated
    if source.embedding is None:
        return []

    # Cosine distance similarity search via pgvector column method
    similar_stmt = (
        select(NewsArticle)
        .options(*_news_eager_options())
        .where(
            NewsArticle.id != news_id,
            NewsArticle.embedding.is_not(None),
        )
        .order_by(NewsArticle.embedding.cosine_distance(source.embedding))
        .limit(limit)
    )

    similar_result = await session.execute(similar_stmt)
    articles = similar_result.unique().scalars().all()

    return [_build_news_response(a, locale=locale) for a in articles]


@router.get("/{news_id}", response_model=NewsResponse)
async def get_news(
    news_id: int,
    locale: str = Query(DEFAULT_LOCALE),
    user: User | None = Depends(get_optional_user),
    session: AsyncSession = Depends(get_session),
) -> NewsResponse:
    stmt = (
        select(NewsArticle)
        .where(NewsArticle.id == news_id)
        .options(*_news_eager_options())
    )
    result = await session.execute(stmt)
    article = result.unique().scalar_one_or_none()

    if not article:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="News article not found",
        )

    watched_ids = await _get_watched_ids(session, user)
    return _build_news_response(article, watched_ids, locale)


@router.post(
    "/fetch",
    response_model=NewsFetchResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def fetch_news(
    body: NewsFetchRequest | None = None,
) -> NewsFetchResponse:
    """Enqueue a news fetch task. Returns immediately with a task ID."""
    source_ids = body.source_ids if body else None
    task = await fetch_and_analyze_task.kiq(source_ids=source_ids)

    return NewsFetchResponse(
        message="Fetch task submitted",
        sources_count=len(source_ids) if source_ids else None,
        job_id=task.task_id,
    )
