"""Helper functions for NewsSource-related queries.

Keeps derived-attribute logic (e.g. last successful fetch time) in one place
so that multiple callers (HN client, AV client, router) share the same query.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models.fetch_log import FetchLog, FetchStatus


async def get_last_successful_fetch_at(
    session: AsyncSession,
    source_id: int,
) -> datetime | None:
    """Return the most recent successful fetch timestamp for *source_id*.

    Derived from ``fetch_logs`` rather than stored on the source model,
    so the value is always consistent with the actual fetch history.
    """
    stmt = select(func.max(FetchLog.fetched_at)).where(
        FetchLog.source_id == source_id,
        FetchLog.status == FetchStatus.SUCCESS,
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()
