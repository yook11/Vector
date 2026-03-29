import math

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlmodel import func, select

from app.dependencies import CurrentUser, get_current_user, get_session
from app.models.news_article import NewsArticle
from app.models.watchlist_entry import WatchlistEntry
from app.schemas.user import (
    WatchlistCreate,
    WatchlistListResponse,
    WatchlistResponse,
)

router = APIRouter(prefix="/api/v1/me", tags=["me"])


# --- Watchlist ---


@router.get("/watchlist", response_model=WatchlistListResponse)
async def list_watchlist(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100, alias="perPage"),
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> WatchlistListResponse:
    base_stmt = select(WatchlistEntry).where(WatchlistEntry.user_id == user.id)

    # Count total
    count_stmt = select(func.count()).select_from(base_stmt.subquery())
    total = (await session.execute(count_stmt)).scalar_one()

    # Fetch paginated items with eager-loaded article + news_source
    offset = (page - 1) * per_page
    stmt = (
        base_stmt.options(
            selectinload(WatchlistEntry.news_article).selectinload(
                NewsArticle.news_source
            )
        )
        .order_by(WatchlistEntry.created_at.desc())
        .offset(offset)
        .limit(per_page)
    )
    result = await session.execute(stmt)
    items = result.scalars().all()

    return WatchlistListResponse(
        items=[
            WatchlistResponse(
                news_article_id=item.news_article.id,
                original_title=item.news_article.original_title,
                # TODO: スキーマ側で SafeUrl を直接受け入れる
                original_url=str(item.news_article.original_url),
                source_name=item.news_article.news_source.name
                if item.news_article.news_source
                else "",
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
    # Verify article exists (eager load news_source for source_name)
    article_stmt = (
        select(NewsArticle)
        .where(NewsArticle.id == body.news_article_id)
        .options(selectinload(NewsArticle.news_source))
    )
    article = (await session.execute(article_stmt)).scalar_one_or_none()
    if not article:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="News article not found",
        )

    # Check duplicate
    existing_stmt = select(WatchlistEntry).where(
        WatchlistEntry.user_id == user.id,
        WatchlistEntry.news_article_id == body.news_article_id,
    )
    existing = (await session.execute(existing_stmt)).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Article already in watchlist",
        )

    item = WatchlistEntry(user_id=user.id, news_article_id=body.news_article_id)
    session.add(item)
    await session.commit()
    await session.refresh(item)

    return WatchlistResponse(
        news_article_id=article.id,
        original_title=article.original_title,
        # TODO: SafeUrl を WatchlistResponse に直接渡せるようスキーマ側を修正する
        original_url=str(article.original_url),
        source_name=article.news_source.name if article.news_source else "",
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
    stmt = select(WatchlistEntry).where(
        WatchlistEntry.user_id == user.id,
        WatchlistEntry.news_article_id == news_article_id,
    )
    item = (await session.execute(stmt)).scalar_one_or_none()
    if not item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Watchlist item not found",
        )

    await session.delete(item)
    await session.commit()
