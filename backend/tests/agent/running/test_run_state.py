"""期限の回収が使うRunStateと、実行世代の読み方(DBを使わない純粋な判定)。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.agent.running.state import RunState, has_attempt_started
from app.agent.running.types import AgentRunStatus

_DEADLINE_AT = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
# 回答開始から45秒(回答生成15秒 + 猶予30秒)が回収期限なので、受理期限より35秒後になる。
_ANSWER_STARTED_AT = _DEADLINE_AT - timedelta(seconds=10)
_RECOVERY_DEADLINE_AT = _DEADLINE_AT + timedelta(seconds=35)
_EPOCH = 2


def _queued() -> RunState:
    return RunState(
        status=AgentRunStatus.QUEUED,
        attempt_epoch=0,
        deadline_at=_DEADLINE_AT,
        answer_started_at=None,
    )


def _answering() -> RunState:
    return RunState(
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
