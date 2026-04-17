"""NewsSource 関連クエリのヘルパー関数群。

派生属性のロジック（例: 直近の成功フェッチ時刻）を 1 箇所に集約し、
複数の呼び出し側（HN クライアント、AV クライアント、router）で
同一のクエリを共有できるようにする。
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
    """指定 ``source_id`` における直近の成功フェッチ時刻を返す。

    ソースモデルに保存するのではなく ``fetch_logs`` から導出するため、
    実際のフェッチ履歴と常に整合が取れる。
    """
    stmt = select(func.max(FetchLog.fetched_at)).where(
        FetchLog.source_id == source_id,
        FetchLog.status == FetchStatus.SUCCESS,
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()
