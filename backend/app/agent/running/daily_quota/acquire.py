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
from app.agent.running.state import DailyLimitReached
from app.models.agent_user_daily_quota import AgentUserDailyQuota


@dataclass(frozen=True, slots=True)
class DailyUsageAcquired:
    usage_date: date
    used_count: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.used_count, int)
            or isinstance(self.used_count, bool)
            or self.used_count < 1
        ):
            raise ValueError("acquired daily usage requires a positive used count")


def _build_acquire_daily_usage_statement(
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
    acquired = (
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
        .cte("acquired")
    )
    return select(
        quota_day.c.observed_at,
        quota_day.c.usage_date,
        func.clock_timestamp().label("decided_at"),
        acquired.c.used_count,
    ).select_from(quota_day.outerjoin(acquired, true()))


async def acquire_daily_usage(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
) -> DailyUsageAcquired | DailyLimitReached:
    """Run1回分の利用枠を確保し、1日の上限に達していれば、上限に達していたことを伝える。"""
    row = (
        (await session.execute(_build_acquire_daily_usage_statement(user_id=user_id)))
        .mappings()
        .one()
    )
    used_count = row["used_count"]
    if used_count is None:
        return DailyLimitReached(
            limit=DAILY_REQUEST_LIMIT,
            usage_date=row["usage_date"],
            decided_at=row["decided_at"],
        )
    return DailyUsageAcquired(
        usage_date=row["usage_date"],
        used_count=used_count,
    )
