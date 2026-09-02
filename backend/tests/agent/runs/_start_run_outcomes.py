"""AgentRunRepository start command outcome のテスト用assertion。"""

from __future__ import annotations

from app.agent.runs.contracts import (
    StartRunCommandOutcome,
    StartRunOutcome,
)
from app.agent.runs.daily_quota.contracts import DailyQuotaReleaseOutcome


def started_attempt_epoch(result: object) -> int:
    """開始成功のcommand outcomeから採番したattempt epochを取り出す。"""
    assert isinstance(result, StartRunCommandOutcome)
    assert result.start_outcome is StartRunOutcome.STARTED
    assert result.quota_release_outcome is None
    assert (
        isinstance(result.attempt_epoch, int)
        and not isinstance(result.attempt_epoch, bool)
        and result.attempt_epoch >= 1
    )
    return result.attempt_epoch


def assert_idempotent_skip(result: object) -> None:
    """開始対象でないrunがquota返却を伴わずskipされたことを確認する。"""
    assert isinstance(result, StartRunCommandOutcome)
    assert result.start_outcome is StartRunOutcome.IDEMPOTENT_SKIP
    assert result.attempt_epoch is None
    assert result.quota_release_outcome is None


def assert_start_deadline_exceeded(
    result: object,
    *,
    quota_release_outcome: DailyQuotaReleaseOutcome,
) -> None:
    """期限超過queued runが実行せずterminal化されたことを確認する。"""
    assert isinstance(result, StartRunCommandOutcome)
    assert result.start_outcome is StartRunOutcome.DEADLINE_EXCEEDED
    assert result.attempt_epoch is None
    assert result.quota_release_outcome is quota_release_outcome
