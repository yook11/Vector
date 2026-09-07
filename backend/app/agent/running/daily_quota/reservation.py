from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import (
    Date,
    Integer,
    Select,
    bindparam,
    cast,
    func,
    literal,
    select,
    true,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.agent.running.daily_quota.policy import (
    DAILY_QUOTA_TIMEZONE_NAME,
    DAILY_REQUEST_LIMIT,
)
from app.models.agent_user_daily_quota import AgentUserDailyQuota


class DailyRequestLimitExceededError(Exception):
    """ユーザーの日次research request予約枠が上限に達した。"""

    def __init__(
        self,
        *,
        usage_date: date,
        observed_at: datetime,
        decided_at: datetime,
        limit: int,
    ) -> None:
        super().__init__("Daily research request limit exceeded")
        self.usage_date = usage_date
        self.observed_at = observed_at
        self.decided_at = decided_at
        self.limit = limit


@dataclass(frozen=True, slots=True)
class DailyQuotaReservation:
    usage_date: date
    used_count: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.used_count, int)
            or isinstance(self.used_count, bool)
            or self.used_count < 1
        ):
            raise ValueError("daily quota reservation requires a positive used count")


def _build_daily_quota_reservation_statement(
    *,
    user_id: uuid.UUID,
    clock_expression: ColumnElement[datetime] | None = None,
) -> Select[tuple[datetime, date, datetime, int | None]]:
    observed_at_expression = (
        clock_expression if clock_expression is not None else func.statement_timestamp()
    )
    quota_clock = (
        select(observed_at_expression.label("observed_at"))
        .cte("quota_clock")
        .prefix_with("MATERIALIZED", dialect="postgresql")
    )
    usage_date_expression = cast(
        quota_clock.c.observed_at.op("AT TIME ZONE")(
            literal(DAILY_QUOTA_TIMEZONE_NAME)
        ),
        Date(),
    ).label("usage_date")
    quota_day = (
        select(quota_clock.c.observed_at, usage_date_expression)
        .select_from(quota_clock)
        .cte("quota_day")
        .prefix_with("MATERIALIZED", dialect="postgresql")
    )
    reservation = (
        pg_insert(AgentUserDailyQuota)
        .from_select(
            ["user_id", "usage_date", "used_count"],
            select(
                bindparam("user_id", user_id, type_=PgUUID(as_uuid=True)),
                quota_day.c.usage_date,
                literal(1, type_=Integer()),
            ).select_from(quota_day),
        )
        .on_conflict_do_update(
            index_elements=[
                AgentUserDailyQuota.user_id,
                AgentUserDailyQuota.usage_date,
            ],
            set_={"used_count": AgentUserDailyQuota.used_count + 1},
            where=(AgentUserDailyQuota.used_count < DAILY_REQUEST_LIMIT),
        )
        .returning(AgentUserDailyQuota.used_count)
        .cte("reservation")
    )
    return select(
        quota_day.c.observed_at,
        quota_day.c.usage_date,
        func.clock_timestamp().label("decided_at"),
        reservation.c.used_count,
    ).select_from(quota_day.outerjoin(reservation, true()))


async def reserve_daily_quota(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
) -> DailyQuotaReservation:
    row = (
        (
            await session.execute(
                _build_daily_quota_reservation_statement(user_id=user_id)
            )
        )
        .mappings()
        .one()
    )
    used_count = row["used_count"]
    if used_count is None:
        raise DailyRequestLimitExceededError(
            usage_date=row["usage_date"],
            observed_at=row["observed_at"],
            decided_at=row["decided_at"],
            limit=DAILY_REQUEST_LIMIT,
        )
    return DailyQuotaReservation(
        usage_date=row["usage_date"],
        used_count=used_count,
    )
