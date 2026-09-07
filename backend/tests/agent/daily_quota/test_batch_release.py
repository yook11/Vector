"""使用枠の一括返却と集計を検証する。"""

from datetime import date
from uuid import UUID

import pytest
from sqlalchemy import event, select

from app.agent.running.daily_quota.release import (
    DailyQuotaBatchReleaseResult,
    DailyQuotaReleaseReservation,
    release_daily_quotas,
)
from app.models.agent_user_daily_quota import AgentUserDailyQuota
from tests.conftest import TEST_ADMIN_ID, TEST_USER_ID

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
USER = UUID(TEST_USER_ID)
DAY = date(2026, 9, 7)


@pytest.mark.parametrize(
    "usage_date,counter,expected,remaining",
    [
        (DAY, 1, (1, 0, 0), 0),
        (None, 1, (0, 1, 0), 1),
        (DAY, 0, (0, 0, 1), 0),
        (DAY, None, (0, 0, 1), None),
    ],
)
async def test_daily_quota_batch_release_policy(
    db_session, usage_date, counter, expected, remaining
):
    # 予約記録なしとカウンター不足・欠損を区別する。
    if counter is not None:
        db_session.add(
            AgentUserDailyQuota(user_id=USER, usage_date=DAY, used_count=counter)
        )
        await db_session.flush()
    result = await release_daily_quotas(
        db_session, reservations=(DailyQuotaReleaseReservation(USER, usage_date),)
    )
    assert result == DailyQuotaBatchReleaseResult(*expected)
    assert await db_session.scalar(select(AgentUserDailyQuota.used_count)) == remaining


async def test_release_groups_by_user_and_day_without_partial_group_release(db_session):
    # 同じ日・ユーザーの件数をまとめ、不足したグループは一部だけ返さない。
    other_user = UUID(TEST_ADMIN_ID)
    other_day = date(2026, 9, 6)
    for user, day, count in [
        (USER, DAY, 3),
        (USER, other_day, 1),
        (other_user, DAY, 1),
    ]:
        db_session.add(
            AgentUserDailyQuota(user_id=user, usage_date=day, used_count=count)
        )
    await db_session.flush()
    reservations = tuple(
        DailyQuotaReleaseReservation(user, day)
        for user, day in [
            (USER, DAY),
            (USER, DAY),
            (USER, other_day),
            (other_user, DAY),
            (other_user, DAY),
        ]
    )
    result = await release_daily_quotas(db_session, reservations=reservations)
    assert result == DailyQuotaBatchReleaseResult(3, 0, 2)
    rows = (
        (
            await db_session.execute(
                select(
                    AgentUserDailyQuota.user_id,
                    AgentUserDailyQuota.usage_date,
                    AgentUserDailyQuota.used_count,
                )
            )
        )
        .tuples()
        .all()
    )
    assert {(user, day): count for user, day, count in rows} == {
        (USER, DAY): 1,
        (USER, other_day): 0,
        (other_user, DAY): 1,
    }


async def test_empty_input_does_not_query_database(db_session):
    # 対象がなければSQLを発行しない。
    statements = []
    engine = db_session.bind.sync_engine

    def record(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        result = await release_daily_quotas(db_session, reservations=())
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert result == DailyQuotaBatchReleaseResult(0, 0, 0)
    assert statements == []
