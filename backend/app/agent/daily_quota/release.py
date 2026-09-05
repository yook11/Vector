from __future__ import annotations

import uuid
from datetime import date
from enum import StrEnum

from sqlalchemy import (
    Integer,
    column,
    update,
    values,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_user_daily_quota import AgentUserDailyQuota


class DailyQuotaReleaseOutcome(StrEnum):
    RELEASED = "released"
    NOT_ELIGIBLE = "not_eligible"
    INCONSISTENT = "inconsistent"


async def release_daily_quota(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    usage_date: date | None,
) -> DailyQuotaReleaseOutcome:
    if usage_date is None:
        return DailyQuotaReleaseOutcome.NOT_ELIGIBLE

    released_user_id = (
        await session.execute(
            update(AgentUserDailyQuota)
            .where(
                AgentUserDailyQuota.user_id == user_id,
                AgentUserDailyQuota.usage_date == usage_date,
                AgentUserDailyQuota.used_count > 0,
            )
            .values(used_count=AgentUserDailyQuota.used_count - 1)
            .returning(AgentUserDailyQuota.user_id)
            .execution_options(synchronize_session=False)
        )
    ).scalar_one_or_none()
    if released_user_id is None:
        return DailyQuotaReleaseOutcome.INCONSISTENT
    return DailyQuotaReleaseOutcome.RELEASED


async def release_daily_quotas(
    session: AsyncSession,
    *,
    reservations: dict[tuple[uuid.UUID, date], int],
) -> set[tuple[uuid.UUID, date]]:
    if not reservations:
        return set()

    quota_releases = values(
        column("user_id", AgentUserDailyQuota.user_id.type),
        column("usage_date", AgentUserDailyQuota.usage_date.type),
        column("release_count", Integer),
        name="daily_quota_releases",
    ).data(
        [
            (user_id, usage_date, count)
            for (user_id, usage_date), count in reservations.items()
        ]
    )
    return set(
        (
            await session.execute(
                update(AgentUserDailyQuota)
                .where(
                    AgentUserDailyQuota.user_id == quota_releases.c.user_id,
                    AgentUserDailyQuota.usage_date == quota_releases.c.usage_date,
                    AgentUserDailyQuota.used_count >= quota_releases.c.release_count,
                )
                .values(
                    used_count=(
                        AgentUserDailyQuota.used_count - quota_releases.c.release_count
                    )
                )
                .returning(
                    AgentUserDailyQuota.user_id,
                    AgentUserDailyQuota.usage_date,
                )
                .execution_options(synchronize_session=False)
            )
        )
        .tuples()
        .all()
    )
