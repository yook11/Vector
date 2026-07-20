"""External search期間解決のbest-effort observability。"""

from __future__ import annotations

from contextlib import suppress
from typing import Literal

import logfire
import structlog

from app.agent.evidence_collection.external_search.contract import (
    TimeFilterFailureReason,
)

TimeFilterResolutionResult = Literal["not_requested", "resolved", "failed"]
TimeFilterResolutionReason = Literal["none"] | TimeFilterFailureReason

logger = structlog.get_logger(__name__)

_time_filter_resolution_counter = logfire.metric_counter(
    "external_search_time_filter_resolution_total",
    unit="1",
    description="External search publication期間の解決結果件数。",
)

_FAILURE_REASONS: tuple[TimeFilterFailureReason, ...] = (
    "future_calendar_month",
    "future_date_range",
    "unexpandable_start_date",
    "unsupported_explicit_window",
)


def record_time_filter_resolution(
    *,
    result: TimeFilterResolutionResult,
    reason: TimeFilterResolutionReason,
) -> None:
    """期間解決結果を閉じた属性だけで記録する。"""
    _time_filter_resolution_counter.add(
        1,
        attributes={"result": result, "reason": reason},
    )


def observe_time_filter_resolution(
    *,
    result: TimeFilterResolutionResult,
    reason: TimeFilterResolutionReason,
    task_count: int,
) -> None:
    """metricとwarningを互いに独立したbest-effort sinkへ送る。"""
    _validate_observation(result=result, reason=reason, task_count=task_count)
    with suppress(Exception):
        record_time_filter_resolution(result=result, reason=reason)
    if result == "failed":
        with suppress(Exception):
            logger.warning(
                "external_search_time_filter_failed",
                reason=reason,
                task_count=task_count,
            )


def _validate_observation(
    *,
    result: TimeFilterResolutionResult,
    reason: TimeFilterResolutionReason,
    task_count: int,
) -> None:
    if result not in ("not_requested", "resolved", "failed"):
        raise ValueError("unsupported time filter resolution result")
    if result == "failed":
        if reason not in _FAILURE_REASONS:
            raise ValueError("failed resolution requires a failure reason")
    elif reason != "none":
        raise ValueError("nonfailed resolution requires reason none")
    if isinstance(task_count, bool) or task_count <= 0:
        raise ValueError("task_count must be a positive integer")
