"""AgentRunRepository acquire command outcome のテスト用assertion。"""

from __future__ import annotations

from typing import cast

import app.agent.runs.contracts as run_contracts
from app.agent.runs.contracts import PreparedAgentRun
from app.agent.runs.daily_quota.contracts import DailyQuotaReleaseOutcome


def acquired_prepared_run(result: object) -> PreparedAgentRun:
    """取得成功のcommand outcomeから実行用runを取り出す。"""
    command_outcome = getattr(run_contracts, "AcquireForExecutionCommandOutcome", None)
    assert command_outcome is not None, (
        "AcquireForExecutionCommandOutcome is not implemented"
    )
    assert isinstance(result, command_outcome)
    acquire_outcome = getattr(run_contracts, "AcquireForExecutionOutcome", None)
    assert acquire_outcome is not None, "AcquireForExecutionOutcome is not implemented"
    assert result.acquire_outcome is acquire_outcome.ACQUIRED
    assert result.quota_release_outcome is None
    assert isinstance(result.prepared_run, PreparedAgentRun)
    return cast(PreparedAgentRun, result.prepared_run)


def assert_idempotent_skip(result: object) -> None:
    """開始対象でないrunがquota返却を伴わずskipされたことを確認する。"""
    command_outcome = getattr(run_contracts, "AcquireForExecutionCommandOutcome", None)
    assert command_outcome is not None, (
        "AcquireForExecutionCommandOutcome is not implemented"
    )
    assert isinstance(result, command_outcome)
    acquire_outcome = getattr(run_contracts, "AcquireForExecutionOutcome", None)
    assert acquire_outcome is not None, "AcquireForExecutionOutcome is not implemented"
    assert result.acquire_outcome is acquire_outcome.IDEMPOTENT_SKIP
    assert result.prepared_run is None
    assert result.quota_release_outcome is None


def assert_queued_start_deadline_expired(
    result: object,
    *,
    quota_release_outcome: DailyQuotaReleaseOutcome,
) -> None:
    """期限超過queued runが実行せずterminal化されたことを確認する。"""
    command_outcome = getattr(run_contracts, "AcquireForExecutionCommandOutcome", None)
    assert command_outcome is not None, (
        "AcquireForExecutionCommandOutcome is not implemented"
    )
    assert isinstance(result, command_outcome)
    acquire_outcome = getattr(run_contracts, "AcquireForExecutionOutcome", None)
    assert acquire_outcome is not None, "AcquireForExecutionOutcome is not implemented"
    assert result.acquire_outcome is acquire_outcome.QUEUED_START_DEADLINE_EXPIRED
    assert result.prepared_run is None
    assert result.quota_release_outcome is quota_release_outcome
