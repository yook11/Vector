from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_user_daily_quota import AgentUserDailyQuota


async def release_daily_usage(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    usage_date: date | None,
) -> None:
    """処理が始まらないまま終了したRunについて、確保したRun1回分の利用枠を解放する。"""
    # 利用枠を確保せずに作られた古いRunは、解放するものが無い。
    if usage_date is None:
        return
    await session.execute(
        update(AgentUserDailyQuota)
        .where(
            AgentUserDailyQuota.user_id == user_id,
            AgentUserDailyQuota.usage_date == usage_date,
            # 利用回数は0未満にできないので、1以上のときだけ減らす。
            AgentUserDailyQuota.used_count > 0,
        )
        .values(used_count=AgentUserDailyQuota.used_count - 1)
        .execution_options(synchronize_session=False)
    )
