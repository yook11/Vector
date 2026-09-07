from __future__ import annotations

import uuid
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class DailyQuotaReleaseReservation:
    user_id: uuid.UUID
    usage_date: date | None


@dataclass(frozen=True, slots=True)
class DailyQuotaBatchReleaseResult:
    released_count: int
    not_eligible_count: int
    inconsistent_count: int


async def release_daily_quotas(
    session: AsyncSession,
    *,
    reservations: tuple[DailyQuotaReleaseReservation, ...],
) -> DailyQuotaBatchReleaseResult:
    """呼び出し元が選んだ予約枠を集計し、同じトランザクションで一括返却する。"""
    groups: dict[tuple[uuid.UUID, date], int] = {}
    not_eligible_count = 0
    for reservation in reservations:
        if reservation.usage_date is None:
            not_eligible_count += 1
            continue
        group = (reservation.user_id, reservation.usage_date)
        groups[group] = groups.get(group, 0) + 1
    released_groups = await _release_daily_quota_groups(session, reservations=groups)
    return DailyQuotaBatchReleaseResult(
        released_count=sum(
            count for group, count in groups.items() if group in released_groups
        ),
        not_eligible_count=not_eligible_count,
        inconsistent_count=sum(
            count for group, count in groups.items() if group not in released_groups
        ),
    )


async def _release_daily_quota_groups(
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
