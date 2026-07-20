"""ユーザー日次利用枠のcommit後observability。"""

from __future__ import annotations

from contextlib import suppress
from datetime import date
from typing import Literal
from uuid import UUID

import logfire
import structlog

from app.agent.runs.daily_quota.contracts import DailyQuotaReleaseOutcome
from app.agent.runs.daily_quota.policy import DAILY_REQUEST_LIMIT

DailyQuotaAdmissionResult = Literal["accepted", "rejected"]
DailyQuotaReleaseResult = Literal["released", "not_eligible", "inconsistent"]
DailyQuotaStalePreviousStatus = Literal["queued", "running"]

logger = structlog.get_logger(__name__)

_daily_quota_admissions_counter = logfire.metric_counter(
    "agent_user_daily_quota_admissions_total",
    unit="1",
    description="ユーザー日次利用枠の受付結果件数。",
)
_daily_quota_releases_counter = logfire.metric_counter(
    "agent_user_daily_quota_releases_total",
    unit="1",
    description="ユーザー日次利用枠の解放結果件数。",
)
_daily_quota_stale_reservations_counter = logfire.metric_counter(
    "agent_user_daily_quota_stale_reservations_total",
    unit="1",
    description="stale run に保持されたユーザー日次利用枠件数。",
)


def record_daily_quota_admission(*, result: DailyQuotaAdmissionResult) -> None:
    """日次利用枠の受付結果を1件記録する。"""
    _daily_quota_admissions_counter.add(1, attributes={"result": result})


def record_daily_quota_release(*, result: DailyQuotaReleaseResult) -> None:
    """日次利用枠の解放結果を1件記録する。"""
    _daily_quota_releases_counter.add(1, attributes={"result": result})


def record_daily_quota_stale_reservation(
    *,
    previous_status: DailyQuotaStalePreviousStatus,
    count: int = 1,
) -> None:
    """stale runに保持された正数の利用枠だけを記録する。"""
    if count > 0:
        _daily_quota_stale_reservations_counter.add(
            count,
            attributes={"previous_status": previous_status},
        )


def observe_admission_accepted(
    *,
    run_id: UUID,
    usage_date: date,
    used_count: int,
) -> None:
    with suppress(Exception):
        logger.info(
            "agent_user_daily_quota_reserved",
            run_id=str(run_id),
            usage_date=usage_date.isoformat(),
            used_count=used_count,
            limit=DAILY_REQUEST_LIMIT,
        )
    with suppress(Exception):
        record_daily_quota_admission(result="accepted")


def observe_admission_rejected(*, usage_date: date) -> None:
    with suppress(Exception):
        logger.info(
            "agent_user_daily_quota_rejected",
            usage_date=usage_date.isoformat(),
            limit=DAILY_REQUEST_LIMIT,
        )
    with suppress(Exception):
        record_daily_quota_admission(result="rejected")


def observe_release(*, run_id: UUID, outcome: DailyQuotaReleaseOutcome) -> None:
    with suppress(Exception):
        record_daily_quota_release(result=outcome.value)

    if outcome is DailyQuotaReleaseOutcome.RELEASED:
        with suppress(Exception):
            logger.info(
                "agent_user_daily_quota_released",
                run_id=str(run_id),
                limit=DAILY_REQUEST_LIMIT,
            )
    elif outcome is DailyQuotaReleaseOutcome.INCONSISTENT:
        with suppress(Exception):
            logger.error(
                "agent_user_daily_quota_release_inconsistent",
                run_id=str(run_id),
                limit=DAILY_REQUEST_LIMIT,
            )


def observe_stale_reservations(*, queued_count: int, running_count: int) -> None:
    if queued_count + running_count <= 0:
        return
    with suppress(Exception):
        logger.warning(
            "agent_user_daily_quota_stale_reservations_retained",
            queued_count=queued_count,
            running_count=running_count,
        )
    with suppress(Exception):
        record_daily_quota_stale_reservation(
            previous_status="queued",
            count=queued_count,
        )
    with suppress(Exception):
        record_daily_quota_stale_reservation(
            previous_status="running",
            count=running_count,
        )
