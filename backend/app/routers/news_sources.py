"""CRUD endpoints for news_sources management."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import func, select

from app.dependencies import CurrentUser, get_admin_user, get_current_user, get_session
from app.models.news_source import NewsSource, SourceType
from app.schemas.news_source import (
    NewsSourceCreate,
    NewsSourceListResponse,
    NewsSourceResponse,
    NewsSourceUpdate,
)

router = APIRouter(prefix="/api/v1/sources", tags=["sources"])


def _to_response(source: NewsSource) -> NewsSourceResponse:
    return NewsSourceResponse(
        id=source.id,
        name=source.name,
        source_type=source.source_type,
        site_url=source.site_url,
        is_active=source.is_active,
        feed_url=source.feed_url,
        api_endpoint=source.api_endpoint,
        fetch_interval_minutes=source.fetch_interval_minutes,
        next_fetch_at=source.next_fetch_at,
        last_fetched_at=source.last_fetched_at,
        consecutive_errors=source.consecutive_errors,
        last_error_message=source.last_error_message,
        created_at=source.created_at,
        updated_at=source.updated_at,
    )


@router.get("", response_model=NewsSourceListResponse)
async def list_sources(
    session: AsyncSession = Depends(get_session),
    _user: CurrentUser = Depends(get_current_user),
) -> NewsSourceListResponse:
    """List all news sources."""
    stmt = select(NewsSource).order_by(NewsSource.name)
    result = await session.execute(stmt)
    sources = result.scalars().all()

    count_stmt = select(func.count()).select_from(NewsSource)
    total = (await session.execute(count_stmt)).scalar_one()

    return NewsSourceListResponse(
        items=[_to_response(s) for s in sources],
        total=total,
    )


@router.get("/{source_id}", response_model=NewsSourceResponse)
async def get_source(
    source_id: int,
    session: AsyncSession = Depends(get_session),
    _user: CurrentUser = Depends(get_current_user),
) -> NewsSourceResponse:
    """Get a single news source by ID."""
    source = await session.get(NewsSource, source_id)
    if source is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="News source not found",
        )
    return _to_response(source)


@router.post(
    "",
    response_model=NewsSourceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_source(
    body: NewsSourceCreate,
    session: AsyncSession = Depends(get_session),
    _user: CurrentUser = Depends(get_admin_user),
) -> NewsSourceResponse:
    """Create a new news source."""
    # Validate type-specific fields
    if body.source_type == SourceType.RSS and not body.feed_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="feed_url is required for RSS sources",
        )
    if body.source_type == SourceType.API and not body.api_endpoint:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="api_endpoint is required for API sources",
        )

    source = NewsSource(
        name=body.name,
        source_type=body.source_type,
        site_url=body.site_url,
        feed_url=body.feed_url if body.source_type == SourceType.RSS else None,
        api_endpoint=body.api_endpoint if body.source_type == SourceType.API else None,
        fetch_interval_minutes=body.fetch_interval_minutes,
    )
    session.add(source)
    await session.commit()
    await session.refresh(source)
    return _to_response(source)


@router.put("/{source_id}", response_model=NewsSourceResponse)
async def update_source(
    source_id: int,
    body: NewsSourceUpdate,
    session: AsyncSession = Depends(get_session),
    _user: CurrentUser = Depends(get_admin_user),
) -> NewsSourceResponse:
    """Update an existing news source."""
    source = await session.get(NewsSource, source_id)
    if source is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="News source not found",
        )

    update_data = body.model_dump(exclude_unset=True)

    # Apply updates
    for key, value in update_data.items():
        setattr(source, key, value)

    source.updated_at = datetime.now(UTC)
    session.add(source)
    await session.commit()
    await session.refresh(source)
    return _to_response(source)


@router.delete(
    "/{source_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_source(
    source_id: int,
    session: AsyncSession = Depends(get_session),
    _user: CurrentUser = Depends(get_admin_user),
) -> None:
    """Delete a news source."""
    source = await session.get(NewsSource, source_id)
    if source is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="News source not found",
        )
    await session.delete(source)
    await session.commit()


@router.patch(
    "/{source_id}/toggle",
    response_model=NewsSourceResponse,
)
async def toggle_source(
    source_id: int,
    session: AsyncSession = Depends(get_session),
    _user: CurrentUser = Depends(get_admin_user),
) -> NewsSourceResponse:
    """Toggle a news source's is_active status."""
    source = await session.get(NewsSource, source_id)
    if source is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="News source not found",
        )

    source.is_active = not source.is_active
    # Reset next_fetch_at when re-enabling (D-2: immediate fetch on reactivation)
    if source.is_active:
        source.next_fetch_at = None
    source.updated_at = datetime.now(UTC)
    session.add(source)
    await session.commit()
    await session.refresh(source)
    return _to_response(source)
