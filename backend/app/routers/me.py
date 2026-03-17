import math

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlmodel import func, select

from app.dependencies import CurrentUser, get_current_user, get_session
from app.models.keyword import Keyword
from app.models.keyword_category import (
    KeywordCategory,
    KeywordCategoryLink,
)
from app.models.news import NewsArticle
from app.models.user_keyword import UserKeywordSubscription
from app.models.watchlist import WatchlistItem
from app.schemas.keyword_category import KeywordCategoryBrief
from app.schemas.user import (
    SubscriptionCreate,
    SubscriptionListResponse,
    SubscriptionResponse,
    WatchlistCreate,
    WatchlistListResponse,
    WatchlistResponse,
)

router = APIRouter(prefix="/api/v1/me", tags=["me"])

DEFAULT_LOCALE = "ja"


def _build_keyword_categories(
    category_links: list[KeywordCategoryLink], locale: str = DEFAULT_LOCALE
) -> list[KeywordCategoryBrief]:
    """Extract translated category briefs from keyword category links."""
    result = []
    for link in category_links:
        if not link.category:
            continue
        name = ""
        for t in link.category.translations:
            if t.locale == locale:
                name = t.name
                break
        result.append(KeywordCategoryBrief(slug=link.category.slug, name=name))
    return result


# --- Subscriptions ---


@router.get("/subscriptions", response_model=SubscriptionListResponse)
async def list_subscriptions(
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> SubscriptionListResponse:
    stmt = (
        select(UserKeywordSubscription)
        .where(UserKeywordSubscription.user_id == user.id)
        .options(
            selectinload(UserKeywordSubscription.keyword)
            .selectinload(Keyword.category_links)
            .selectinload(KeywordCategoryLink.category)
            .selectinload(KeywordCategory.translations)
        )
        .order_by(UserKeywordSubscription.created_at.desc())
    )
    result = await session.execute(stmt)
    subs = result.unique().scalars().all()

    return SubscriptionListResponse(
        items=[
            SubscriptionResponse(
                id=sub.id,
                keyword_id=sub.keyword.id,
                keyword=sub.keyword.keyword,
                categories=_build_keyword_categories(sub.keyword.category_links),
                created_at=sub.created_at,
            )
            for sub in subs
        ]
    )


@router.post(
    "/subscriptions",
    response_model=SubscriptionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_subscription(
    body: SubscriptionCreate,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> SubscriptionResponse:
    # Verify keyword exists (with category links for response)
    stmt = (
        select(Keyword)
        .where(Keyword.id == body.keyword_id)
        .options(
            selectinload(Keyword.category_links)
            .selectinload(KeywordCategoryLink.category)
            .selectinload(KeywordCategory.translations)
        )
    )
    result = await session.execute(stmt)
    keyword = result.unique().scalar_one_or_none()
    if not keyword:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Keyword not found",
        )

    # Check duplicate
    existing_stmt = select(UserKeywordSubscription).where(
        UserKeywordSubscription.user_id == user.id,
        UserKeywordSubscription.keyword_id == body.keyword_id,
    )
    existing = (await session.execute(existing_stmt)).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Already subscribed to this keyword",
        )

    sub = UserKeywordSubscription(user_id=user.id, keyword_id=body.keyword_id)
    session.add(sub)
    await session.commit()
    await session.refresh(sub)

    return SubscriptionResponse(
        id=sub.id,
        keyword_id=keyword.id,
        keyword=keyword.keyword,
        categories=_build_keyword_categories(keyword.category_links),
        created_at=sub.created_at,
    )


@router.delete(
    "/subscriptions/{keyword_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_subscription(
    keyword_id: int,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    stmt = select(UserKeywordSubscription).where(
        UserKeywordSubscription.user_id == user.id,
        UserKeywordSubscription.keyword_id == keyword_id,
    )
    sub = (await session.execute(stmt)).scalar_one_or_none()
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Subscription not found",
        )

    await session.delete(sub)
    await session.commit()


# --- Watchlist ---


@router.get("/watchlist", response_model=WatchlistListResponse)
async def list_watchlist(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100, alias="perPage"),
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> WatchlistListResponse:
    base_stmt = select(WatchlistItem).where(WatchlistItem.user_id == user.id)

    # Count total
    count_stmt = select(func.count()).select_from(base_stmt.subquery())
    total = (await session.execute(count_stmt)).scalar_one()

    # Fetch paginated items with eager-loaded article
    offset = (page - 1) * per_page
    stmt = (
        base_stmt.options(selectinload(WatchlistItem.news_article))
        .order_by(WatchlistItem.created_at.desc())
        .offset(offset)
        .limit(per_page)
    )
    result = await session.execute(stmt)
    items = result.scalars().all()

    return WatchlistListResponse(
        items=[
            WatchlistResponse(
                id=item.id,
                news_article_id=item.news_article.id,
                title_original=item.news_article.title_original,
                url=item.news_article.url,
                source=item.news_article.source,
                published_at=item.news_article.published_at,
                created_at=item.created_at,
            )
            for item in items
        ],
        total=total,
        page=page,
        per_page=per_page,
        total_pages=math.ceil(total / per_page) if total > 0 else 0,
    )


@router.post(
    "/watchlist",
    response_model=WatchlistResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_to_watchlist(
    body: WatchlistCreate,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> WatchlistResponse:
    # Verify article exists
    article = await session.get(NewsArticle, body.news_article_id)
    if not article:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="News article not found",
        )

    # Check duplicate
    existing_stmt = select(WatchlistItem).where(
        WatchlistItem.user_id == user.id,
        WatchlistItem.news_article_id == body.news_article_id,
    )
    existing = (await session.execute(existing_stmt)).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Article already in watchlist",
        )

    item = WatchlistItem(user_id=user.id, news_article_id=body.news_article_id)
    session.add(item)
    await session.commit()
    await session.refresh(item)

    return WatchlistResponse(
        id=item.id,
        news_article_id=article.id,
        title_original=article.title_original,
        url=article.url,
        source=article.source,
        published_at=article.published_at,
        created_at=item.created_at,
    )


@router.delete(
    "/watchlist/{news_article_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_from_watchlist(
    news_article_id: int,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    stmt = select(WatchlistItem).where(
        WatchlistItem.user_id == user.id,
        WatchlistItem.news_article_id == news_article_id,
    )
    item = (await session.execute(stmt)).scalar_one_or_none()
    if not item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Watchlist item not found",
        )

    await session.delete(item)
    await session.commit()
