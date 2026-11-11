"""RunState の段階と、概念ごとの判定(DB を使わない純粋な判定)。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from app.agent.running.state import (
    AnswerAlreadyStarted,
    AttemptAlreadyStarted,
    AttemptSuperseded,
    CancelledByUser,
    CanContinue,
    CannotContinue,
    DeadlineExceeded,
    RunAlreadyCancelled,
    RunAlreadyCompleted,
    RunAlreadyDeadlineExceeded,
    RunAlreadyFailed,
    RunNotFound,
    RunPhase,
    RunPhaseMismatchError,
    RunState,
    active_run_state,
    has_attempt_started,
)
from app.agent.running.types import AgentRunErrorCode, AgentRunStatus

_DEADLINE_AT = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
# 回答開始から45秒(回答生成15秒 + 猶予30秒)が回収期限なので、受理期限より35秒後になる。
_ANSWER_STARTED_AT = _DEADLINE_AT - timedelta(seconds=10)
_RECOVERY_DEADLINE_AT = _DEADLINE_AT + timedelta(seconds=35)
_EPOCH = 2

Check = Callable[[RunState, datetime], object]


def _queued() -> RunState:
    return RunState(
        status=AgentRunStatus.QUEUED,
        attempt_epoch=0,
        deadline_at=_DEADLINE_AT,
        answer_started_at=None,
    )


def _preparing(*, attempt_epoch: int = _EPOCH) -> RunState:
    return RunState(
        status=AgentRunStatus.RUNNING,
        attempt_epoch=attempt_epoch,
        deadline_at=_DEADLINE_AT,
        answer_started_at=None,
    )


def _answering(*, attempt_epoch: int = _EPOCH) -> RunState:
    return RunState(
        status=AgentRunStatus.RUNNING,
        attempt_epoch=attempt_epoch,
        deadline_at=_DEADLINE_AT,
        answer_started_at=_ANSWER_STARTED_AT,
    )


def _start(state: RunState, now: datetime) -> object:
    return state.check_start(now=now)


def _attempt(state: RunState, now: datetime) -> object:
    return state.check_attempt(
        attempt_epoch=_EPOCH,
        phases={RunPhase.PREPARING, RunPhase.ANSWERING},
        now=now,
    )


def _cancel(state: RunState, now: datetime) -> object:
    return state.check_cancel(now=now)


def _enqueue_failure(state: RunState, now: datetime) -> object:
    return state.check_enqueue_failure(now=now)


@pytest.mark.parametrize(
    ("status", "error_code", "expected"),
    [
        pytest.param(
            AgentRunStatus.FAILED,
            AgentRunErrorCode.CANCELLED,
            RunAlreadyCancelled(),
            id="cancelled",
        ),
        pytest.param(
            AgentRunStatus.DEADLINE_EXCEEDED,
            None,
            RunAlreadyDeadlineExceeded(),
            id="deadline_exceeded",
        ),
        pytest.param(
            AgentRunStatus.COMPLETED, None, RunAlreadyCompleted(), id="completed"
        ),
        pytest.param(
            AgentRunStatus.FAILED,
            AgentRunErrorCode.GENERATION_UNAVAILABLE,
            RunAlreadyFailed(),
            id="failed",
        ),
        pytest.param(
            AgentRunStatus.POLICY_BLOCKED, None, RunAlreadyFailed(), id="policy_blocked"
        ),
    ],
)
def test_ended_run_is_read_as_how_it_ended(
    status: AgentRunStatus, error_code: AgentRunErrorCode | None, expected: object
) -> None:
    """終わったRunの保存値は、状態ではなく終わり方(停止・期限切れ・完了・失敗)として読まれる。"""
    assert (
        active_run_state(
            status=status,
            error_code=error_code,
            attempt_epoch=_EPOCH,
            deadline_at=_DEADLINE_AT,
            answer_started_at=_ANSWER_STARTED_AT,
        )
        == expected
    )


def test_active_run_is_read_as_its_state() -> None:
    """動いているRunの保存値は、状態・世代・受理期限・回答開始時刻を持つ状態として読まれる。"""
    assert active_run_state(
        status=AgentRunStatus.RUNNING,
        error_code=None,
        attempt_epoch=_EPOCH,
        deadline_at=_DEADLINE_AT,
        answer_started_at=_ANSWER_STARTED_AT,
    ) == RunState(
        status=AgentRunStatus.RUNNING,
        attempt_epoch=_EPOCH,
        deadline_at=_DEADLINE_AT,
        answer_started_at=_ANSWER_STARTED_AT,
    )


@pytest.mark.parametrize(
    "status",
    [
        AgentRunStatus.COMPLETED,
        AgentRunStatus.FAILED,
        AgentRunStatus.POLICY_BLOCKED,
        AgentRunStatus.DEADLINE_EXCEEDED,
    ],
)
def test_state_cannot_be_built_for_ended_run(status: AgentRunStatus) -> None:
    """終わったRunの状態を作ろうとすると、誤りとして例外になる。"""
    with pytest.raises(ValueError):
        RunState(
            status=status,
            attempt_epoch=_EPOCH,
            deadline_at=_DEADLINE_AT,
            answer_started_at=_ANSWER_STARTED_AT,
        )


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        pytest.param(_queued(), RunPhase.QUEUED, id="queued"),
        pytest.param(_preparing(), RunPhase.PREPARING, id="running-before-answer"),
        pytest.param(_answering(), RunPhase.ANSWERING, id="running-after-answer"),
    ],
)
def test_phase_follows_status_and_answer_start(
    state: RunState, expected: RunPhase
) -> None:
    """動いているRunの段階は、状態と回答開始の有無から待機中・準備中・回答中のどれか1つに決まる。"""
    assert state.phase is expected


def test_stale_attempt_is_superseded_without_expiring_run() -> None:
    """古い世代の試行の判定は、期限を過ぎていても期限切れにせず別の試行が担当していると返る。"""
    assert (
        _attempt(
            _answering(attempt_epoch=_EPOCH + 1),
            _RECOVERY_DEADLINE_AT + timedelta(seconds=1),
        )
        == AttemptSuperseded()
    )


def test_start_allows_queued_run_before_deadline() -> None:
    """受理期限の1秒前のqueuedのRunは開始できる。"""
    assert _queued().check_start(now=_DEADLINE_AT - timedelta(seconds=1)) == (
        CanContinue()
    )


def test_start_expires_queued_run_at_exact_deadline() -> None:
    """受理期限ちょうどに開始しようとしたqueuedのRunは期限切れになる。"""
    assert _queued().check_start(now=_DEADLINE_AT) == DeadlineExceeded()


def test_start_allows_redelivered_run_before_answer() -> None:
    """回答を始める前に再配信されたRunは、受理期限内なら開始し直せる。"""
    assert _preparing().check_start(now=_DEADLINE_AT - timedelta(seconds=1)) == (
        CanContinue()
    )


def test_start_yields_to_answering_attempt_without_expiring_it_after_deadline() -> None:
    """回答中のRunへ受理期限後に再配信が届いても、期限切れにせず回答が始まっていると返る。"""
    assert _answering().check_start(now=_DEADLINE_AT + timedelta(seconds=1)) == (
        AnswerAlreadyStarted()
    )


@pytest.mark.parametrize("check", [_attempt, _cancel])
def test_answering_run_is_allowed_until_recovery_deadline(
    check: Check,
) -> None:
    """回答中のRunへの操作の判定は、受理期限を過ぎても回収期限の1秒前まで許される。"""
    assert check(_answering(), _RECOVERY_DEADLINE_AT - timedelta(seconds=1)) == (
        CanContinue()
    )


@pytest.mark.parametrize("check", [_attempt, _cancel])
def test_answering_run_expires_at_exact_recovery_deadline(
    check: Check,
) -> None:
    """回答中のRunへの操作の判定は、回収期限ちょうどで期限切れになる。"""
    assert check(_answering(), _RECOVERY_DEADLINE_AT) == DeadlineExceeded()


@pytest.mark.parametrize(
    ("state", "check"),
    [
        pytest.param(_preparing(), _attempt, id="attempt"),
        pytest.param(_queued(), _cancel, id="cancel"),
        pytest.param(_queued(), _enqueue_failure, id="enqueue_failure"),
    ],
)
def test_run_before_answer_expires_at_exact_deadline(
    state: RunState, check: Check
) -> None:
    """回答を始めていないRunへの操作の判定は、受理期限ちょうどで期限切れになる。"""
    assert check(state, _DEADLINE_AT) == DeadlineExceeded()


@pytest.mark.parametrize(
    ("state", "check"),
    [
        pytest.param(
            _answering(),
            lambda state, now: state.check_attempt(
                attempt_epoch=_EPOCH, phases={RunPhase.PREPARING}, now=now
            ),
            id="answering-where-preparing-required",
        ),
        pytest.param(
            _preparing(),
            lambda state, now: state.check_attempt(
                attempt_epoch=_EPOCH, phases={RunPhase.ANSWERING}, now=now
            ),
            id="preparing-where-answering-required",
        ),
    ],
)
def test_current_attempt_outside_allowed_phases_is_a_bug(
    state: RunState, check: Check
) -> None:
    """現在の試行が許されない段階で操作すると、呼び出し順の誤りとして例外になる。"""
    with pytest.raises(RunPhaseMismatchError):
        check(state, _DEADLINE_AT - timedelta(seconds=1))


def test_enqueue_failure_is_allowed_for_queued_run_before_deadline() -> None:
    """待機中のRunの投入の失敗は、受理期限の1秒前なら記録を許される。"""
    assert _queued().check_enqueue_failure(now=_DEADLINE_AT - timedelta(seconds=1)) == (
        CanContinue()
    )


@pytest.mark.parametrize("state", [_preparing(), _answering()])
def test_enqueue_failure_sees_attempt_already_started(state: RunState) -> None:
    """投入の失敗を記録しようとしたRunをworkerが既に始めていたら、試行開始済みと返る。"""
    assert (
        state.check_enqueue_failure(now=_DEADLINE_AT - timedelta(seconds=1))
        == AttemptAlreadyStarted()
    )


@pytest.mark.parametrize(
    ("state", "now", "expected"),
    [
        pytest.param(_queued(), _DEADLINE_AT, True, id="queued-at-deadline"),
        pytest.param(
            _answering(),
            _DEADLINE_AT,
            False,
            id="answering-after-acceptance-deadline",
        ),
        pytest.param(
            _answering(),
            _RECOVERY_DEADLINE_AT,
            True,
            id="answering-at-recovery-deadline",
        ),
    ],
)
def test_deadline_recovery_targets_active_runs_past_recovery_deadline(
    state: RunState, now: datetime, expected: bool
) -> None:
    """動いているRunは、回収期限に達したときだけ期限回収の対象になる。"""
    assert state.is_due_for_deadline_recovery(now=now) is expected


@pytest.mark.parametrize(("attempt_epoch", "expected"), [(0, False), (1, True)])
def test_attempt_started_only_after_first_epoch(
    attempt_epoch: int, expected: bool
) -> None:
    """実行世代が0のRunはworkerがまだ開始しておらず、1以上なら開始済みと読む。"""
    assert has_attempt_started(attempt_epoch) is expected


@pytest.mark.parametrize(
    "stopped",
    [
        DeadlineExceeded(),
        AttemptSuperseded(),
        AnswerAlreadyStarted(),
        RunNotFound(),
        RunAlreadyCompleted(),
        RunAlreadyFailed(),
        RunAlreadyCancelled(),
        RunAlreadyDeadlineExceeded(),
    ],
)
def test_every_stop_reason_is_received_as_cannot_continue(stopped: object) -> None:
    """止まった理由の型は、どれも「続けられない」として受けられる。"""
    assert isinstance(stopped, CannotContinue)


@pytest.mark.parametrize(
    "outcome",
    [
        CanContinue(),
        CancelledByUser(),
        AttemptAlreadyStarted(),
    ],
)
def test_other_outcomes_are_not_received_as_cannot_continue(outcome: object) -> None:
    """続けてよい・この操作による停止・試行開始済みは、「続けられない」としては受けられない。"""
    assert not isinstance(outcome, CannotContinue)
