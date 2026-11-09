"""RunState の段階と、概念ごとの判定(DB を使わない純粋な判定)。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from app.agent.running.state import (
    AttemptAlreadyStarted,
    AttemptSuperseded,
    CancelledByUser,
    CanContinue,
    DeadlineExceeded,
    RunAlreadyCompleted,
    RunAlreadyFailed,
    RunPhase,
    RunPhaseMismatchError,
    RunState,
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
        error_code=None,
        attempt_epoch=0,
        deadline_at=_DEADLINE_AT,
        answer_started_at=None,
    )


def _preparing(*, attempt_epoch: int = _EPOCH) -> RunState:
    return RunState(
        status=AgentRunStatus.RUNNING,
        error_code=None,
        attempt_epoch=attempt_epoch,
        deadline_at=_DEADLINE_AT,
        answer_started_at=None,
    )


def _answering(*, attempt_epoch: int = _EPOCH) -> RunState:
    return RunState(
        status=AgentRunStatus.RUNNING,
        error_code=None,
        attempt_epoch=attempt_epoch,
        deadline_at=_DEADLINE_AT,
        answer_started_at=_ANSWER_STARTED_AT,
    )


def _finished(
    status: AgentRunStatus, error_code: AgentRunErrorCode | None = None
) -> RunState:
    return RunState(
        status=status,
        error_code=error_code,
        attempt_epoch=_EPOCH,
        deadline_at=_DEADLINE_AT,
        answer_started_at=_ANSWER_STARTED_AT,
    )


def _start(state: RunState, now: datetime) -> object:
    return state.check_start(now=now)


def _progress(state: RunState, now: datetime) -> object:
    return state.check_progress(
        attempt_epoch=_EPOCH,
        phases={RunPhase.PREPARING, RunPhase.ANSWERING},
        now=now,
    )


def _attempt_finish(state: RunState, now: datetime) -> object:
    return state.check_attempt_finish(
        attempt_epoch=_EPOCH,
        phases={RunPhase.PREPARING, RunPhase.ANSWERING},
        now=now,
    )


def _finish(state: RunState, now: datetime) -> object:
    return state.check_finish(
        phases={RunPhase.QUEUED, RunPhase.PREPARING, RunPhase.ANSWERING},
        now=now,
    )


_FINISHED_CANCELLED = _finished(AgentRunStatus.FAILED, AgentRunErrorCode.CANCELLED)
_FINISHED_DEADLINE = _finished(AgentRunStatus.DEADLINE_EXCEEDED)
_FINISHED_COMPLETED = _finished(AgentRunStatus.COMPLETED)
_FINISHED_FAILED = _finished(
    AgentRunStatus.FAILED, AgentRunErrorCode.GENERATION_UNAVAILABLE
)
_FINISHED_POLICY_BLOCKED = _finished(AgentRunStatus.POLICY_BLOCKED)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        pytest.param(_queued(), RunPhase.QUEUED, id="queued"),
        pytest.param(_preparing(), RunPhase.PREPARING, id="running-before-answer"),
        pytest.param(_answering(), RunPhase.ANSWERING, id="running-after-answer"),
        pytest.param(_FINISHED_COMPLETED, RunPhase.FINISHED, id="completed"),
        pytest.param(_FINISHED_FAILED, RunPhase.FINISHED, id="failed"),
        pytest.param(_FINISHED_POLICY_BLOCKED, RunPhase.FINISHED, id="policy_blocked"),
        pytest.param(_FINISHED_DEADLINE, RunPhase.FINISHED, id="deadline_exceeded"),
    ],
)
def test_phase_follows_status_and_answer_start(
    state: RunState, expected: RunPhase
) -> None:
    """Runの段階は、状態と回答開始の有無から待機中・準備中・回答中・終了のどれか1つに決まる。"""
    assert state.phase is expected


@pytest.mark.parametrize("check", [_start, _progress, _attempt_finish])
@pytest.mark.parametrize(
    ("state", "expected"),
    [
        pytest.param(_FINISHED_CANCELLED, CancelledByUser(), id="cancelled"),
        pytest.param(_FINISHED_DEADLINE, DeadlineExceeded(), id="deadline_exceeded"),
        pytest.param(_FINISHED_COMPLETED, AttemptSuperseded(), id="completed"),
        pytest.param(_FINISHED_FAILED, AttemptSuperseded(), id="failed"),
        pytest.param(
            _FINISHED_POLICY_BLOCKED, AttemptSuperseded(), id="policy_blocked"
        ),
    ],
)
def test_worker_sees_how_finished_run_ended_before_deadline_is_considered(
    check: Check, state: RunState, expected: object
) -> None:
    """終了済みのRunを試行が判定すると、期限を過ぎていても終わり方(停止・期限切れ・別の試行が担当)が返る。"""
    assert check(state, _RECOVERY_DEADLINE_AT + timedelta(seconds=1)) == expected


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        pytest.param(_FINISHED_CANCELLED, CancelledByUser(), id="cancelled"),
        pytest.param(_FINISHED_DEADLINE, DeadlineExceeded(), id="deadline_exceeded"),
        pytest.param(_FINISHED_COMPLETED, RunAlreadyCompleted(), id="completed"),
        pytest.param(_FINISHED_FAILED, RunAlreadyFailed(), id="failed"),
        pytest.param(_FINISHED_POLICY_BLOCKED, RunAlreadyFailed(), id="policy_blocked"),
    ],
)
def test_caller_without_attempt_sees_how_finished_run_ended(
    state: RunState, expected: object
) -> None:
    """終了済みのRunを世代を持たない主体が判定すると、完了済み・失敗済みを含む終わり方が返る。"""
    assert _finish(state, _RECOVERY_DEADLINE_AT + timedelta(seconds=1)) == expected


@pytest.mark.parametrize("check", [_progress, _attempt_finish])
def test_stale_attempt_is_superseded_without_expiring_run(check: Check) -> None:
    """古い世代の試行の判定は、期限を過ぎていても期限切れにせず別の試行が担当していると返る。"""
    assert (
        check(
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
    """回答中のRunへ受理期限後に再配信が届いても、期限切れにせず別の試行が担当していると返る。"""
    assert _answering().check_start(now=_DEADLINE_AT + timedelta(seconds=1)) == (
        AttemptSuperseded()
    )


def test_progress_while_answering_is_limited_by_acceptance_deadline() -> None:
    """回答中でも、受理期限を過ぎてから作業を進めようとすると期限切れになる。"""
    assert (
        _answering().check_progress(
            attempt_epoch=_EPOCH, phases={RunPhase.ANSWERING}, now=_DEADLINE_AT
        )
        == DeadlineExceeded()
    )


@pytest.mark.parametrize("check", [_attempt_finish, _finish])
def test_finishing_answering_run_is_allowed_until_recovery_deadline(
    check: Check,
) -> None:
    """回答中のRunを終える判定は、受理期限を過ぎても回収期限の1秒前まで許される。"""
    assert check(_answering(), _RECOVERY_DEADLINE_AT - timedelta(seconds=1)) == (
        CanContinue()
    )


@pytest.mark.parametrize("check", [_attempt_finish, _finish])
def test_finishing_answering_run_expires_at_exact_recovery_deadline(
    check: Check,
) -> None:
    """回答中のRunを終える判定は、回収期限ちょうどで期限切れになる。"""
    assert check(_answering(), _RECOVERY_DEADLINE_AT) == DeadlineExceeded()


@pytest.mark.parametrize(
    ("state", "check"),
    [
        pytest.param(_preparing(), _attempt_finish, id="attempt_finish"),
        pytest.param(_queued(), _finish, id="finish"),
    ],
)
def test_finishing_run_before_answer_expires_at_exact_deadline(
    state: RunState, check: Check
) -> None:
    """回答を始めていないRunを終える判定は、受理期限ちょうどで期限切れになる。"""
    assert check(state, _DEADLINE_AT) == DeadlineExceeded()


@pytest.mark.parametrize(
    ("state", "check"),
    [
        pytest.param(
            _answering(),
            lambda state, now: state.check_progress(
                attempt_epoch=_EPOCH, phases={RunPhase.PREPARING}, now=now
            ),
            id="answering-where-preparing-required",
        ),
        pytest.param(
            _preparing(),
            lambda state, now: state.check_progress(
                attempt_epoch=_EPOCH, phases={RunPhase.ANSWERING}, now=now
            ),
            id="preparing-where-answering-required-to-progress",
        ),
        pytest.param(
            _preparing(),
            lambda state, now: state.check_attempt_finish(
                attempt_epoch=_EPOCH, phases={RunPhase.ANSWERING}, now=now
            ),
            id="preparing-where-answering-required-to-finish",
        ),
    ],
)
def test_current_attempt_outside_allowed_phases_is_a_bug(
    state: RunState, check: Check
) -> None:
    """現在の試行が許されない段階で操作すると、呼び出し順の誤りとして例外になる。"""
    with pytest.raises(RunPhaseMismatchError):
        check(state, _DEADLINE_AT - timedelta(seconds=1))


@pytest.mark.parametrize("state", [_preparing(), _answering()])
def test_finish_limited_to_queued_sees_attempt_already_started(state: RunState) -> None:
    """待機中に限って終えようとしたRunをworkerが既に始めていたら、試行開始済みと返る。"""
    assert (
        state.check_finish(
            phases={RunPhase.QUEUED}, now=_DEADLINE_AT - timedelta(seconds=1)
        )
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
        pytest.param(
            _FINISHED_COMPLETED,
            _RECOVERY_DEADLINE_AT,
            False,
            id="completed",
        ),
    ],
)
def test_deadline_recovery_targets_active_runs_past_recovery_deadline(
    state: RunState, now: datetime, expected: bool
) -> None:
    """期限回収の対象は、回収期限に達した実行中のRunだけになる。"""
    assert state.is_due_for_deadline_recovery(now=now) is expected


@pytest.mark.parametrize(("attempt_epoch", "expected"), [(0, False), (1, True)])
def test_attempt_started_only_after_first_epoch(
    attempt_epoch: int, expected: bool
) -> None:
    """実行世代が0のRunはworkerがまだ開始しておらず、1以上なら開始済みと読む。"""
    assert has_attempt_started(attempt_epoch) is expected
