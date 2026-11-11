"""ユーザー日次利用枠のcommit後のログ。"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from app.agent.running.daily_quota.policy import DAILY_REQUEST_LIMIT
from app.log_policy.policies.research import RESEARCH_LOG_RULES
from app.log_policy.runtime import create_policy_json_logger

logger = create_policy_json_logger(__name__, RESEARCH_LOG_RULES)


def observe_admission_accepted(
    *,
    run_id: UUID,
    usage_date: date,
    used_count: int,
) -> None:
    logger.info(
        "agent_user_daily_quota_reserved",
        run_id=str(run_id),
        usage_date=usage_date.isoformat(),
        used_count=used_count,
        limit=DAILY_REQUEST_LIMIT,
    )


def observe_admission_rejected(*, usage_date: date) -> None:
    logger.info(
        "agent_user_daily_quota_rejected",
        usage_date=usage_date.isoformat(),
        limit=DAILY_REQUEST_LIMIT,
    )


def observe_stale_reservations(*, queued_count: int, running_count: int) -> None:
    if queued_count + running_count <= 0:
        return
    logger.warning(
        "agent_user_daily_quota_stale_reservations_retained",
        queued_count=queued_count,
        running_count=running_count,
    )
