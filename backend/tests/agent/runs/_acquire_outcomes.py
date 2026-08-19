"""AgentRunRepository acquire command outcome のテスト用assertion。"""

from __future__ import annotations

from app.agent.runs.contracts import (
    AcquireForExecutionCommandOutcome,
    AcquireForExecutionOutcome,
)
from app.agent.runs.daily_quota.contracts import DailyQuotaReleaseOutcome


def acquired_attempt_epoch(result: object) -> int:
    """取得成功のcommand outcomeから採番したattempt epochを取り出す。"""
    assert isinstance(result, AcquireForExecutionCommandOutcome)
    assert result.acquire_outcome is AcquireForExecutionOutcome.ACQUIRED
    assert result.quota_release_outcome is None
    assert (
        isinstance(result.attempt_epoch, int)
        and not isinstance(result.attempt_epoch, bool)
        and result.attempt_epoch >= 1
    )
    return result.attempt_epoch


def assert_idempotent_skip(result: object) -> None:
    """開始対象でないrunがquota返却を伴わずskipされたことを確認する。"""
    assert isinstance(result, AcquireForExecutionCommandOutcome)
    assert result.acquire_outcome is AcquireForExecutionOutcome.IDEMPOTENT_SKIP
    assert result.attempt_epoch is None
    assert result.quota_release_outcome is None


def assert_queued_start_deadline_expired(
    result: object,
    *,
    quota_release_outcome: DailyQuotaReleaseOutcome,
) -> None:
    """期限超過queued runが実行せずterminal化されたことを確認する。"""
    assert isinstance(result, AcquireForExecutionCommandOutcome)
    assert (
        result.acquire_outcome
        is AcquireForExecutionOutcome.QUEUED_START_DEADLINE_EXPIRED
    )
    assert result.attempt_epoch is None
    assert result.quota_release_outcome is quota_release_outcome
