"""ユーザー日次利用枠の状態遷移を集計する metric。"""

from __future__ import annotations

from typing import Literal

import logfire

DailyQuotaAdmissionResult = Literal["accepted", "rejected"]
DailyQuotaReleaseResult = Literal["released", "not_eligible", "inconsistent"]
DailyQuotaStalePreviousStatus = Literal["queued", "running"]

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
    """日次利用枠の受付結果を 1 件記録する。"""
    _daily_quota_admissions_counter.add(1, attributes={"result": result})


def record_daily_quota_release(*, result: DailyQuotaReleaseResult) -> None:
    """日次利用枠の解放結果を 1 件記録する。"""
    _daily_quota_releases_counter.add(1, attributes={"result": result})


def record_daily_quota_stale_reservation(
    *,
    previous_status: DailyQuotaStalePreviousStatus,
    count: int = 1,
) -> None:
    """stale run に保持された正数の利用枠だけを記録する。"""
    if count > 0:
        _daily_quota_stale_reservations_counter.add(
            count,
            attributes={"previous_status": previous_status},
        )
