"""Runの状態を変える8操作が、どの状態・時刻・世代のRunに書けるかの表(実DB、時刻は注入)。"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.contract import AnswerPlanSummary, AnswerQuestionResult
from app.agent.running.state import (
    AnswerGenerationStarted,
    AttemptStarted,
    CancelledByUser,
    CanContinue,
    CannotContinue,
    RunCompleted,
    RunFailed,
    RunNotFound,
)
from app.agent.running.types import AgentRunErrorCode, AgentRunStatus, RunAttempt
from app.models.agent_run import AgentRun
from app.models.agent_user_daily_quota import AgentUserDailyQuota
from tests.agent.running._seed import create_thread_message_run
from tests.agent.running._state_service import (
    ClosedRunningRun,
    ClosedRunNotices,
    state_service,
)
from tests.conftest import TEST_USER_ID

_USER_ID = uuid.UUID(TEST_USER_ID)
_USAGE_DATE = date(2026, 11, 11)
_DEADLINE_AT = datetime(2026, 11, 11, 3, 1, tzinfo=UTC)
_CREATED_AT = _DEADLINE_AT - timedelta(seconds=60)
_JUST_BEFORE_DEADLINE = _DEADLINE_AT - timedelta(microseconds=1)
_ANSWER_STARTED_AT = _DEADLINE_AT - timedelta(seconds=5)
# 回収期限は回答開始から45秒(回答の工程15秒と回収の猶予30秒)で、受理期限より後にある。
_RECOVERY_DEADLINE_AT = _ANSWER_STARTED_AT + timedelta(seconds=45)
_JUST_BEFORE_RECOVERY_DEADLINE = _RECOVERY_DEADLINE_AT - timedelta(microseconds=1)

_QUEUED: dict[str, Any] = {"status": "queued", "attempt_epoch": 0}
_PREPARING: dict[str, Any] = {"status": "running", "attempt_epoch": 1}
_ANSWERING: dict[str, Any] = {
    "status": "running",
    "attempt_epoch": 1,
    "answer_started_at": _ANSWER_STARTED_AT,
}
_TAKEN_OVER: dict[str, Any] = {"status": "running", "attempt_epoch": 2}
_TAKEN_OVER_ANSWERING: dict[str, Any] = {
    "status": "running",
    "attempt_epoch": 2,
    "answer_started_at": _ANSWER_STARTED_AT,
}
_FAILED: dict[str, Any] = {
    "status": "failed",
    "attempt_epoch": 1,
    "error_code": "internal_error",
}
_CANCELLED: dict[str, Any] = {
    "status": "failed",
    "attempt_epoch": 1,
    "error_code": "cancelled",
}
_EXPIRED: dict[str, Any] = {"status": "deadline_exceeded", "attempt_epoch": 1}

type _Row = tuple[str, str | None, int, datetime | None]


async def _seed(
    session_factory: async_sessionmaker[AsyncSession], seed: dict[str, Any]
) -> uuid.UUID:
    async with session_factory() as session:
        session.add(
            AgentUserDailyQuota(user_id=_USER_ID, usage_date=_USAGE_DATE, used_count=1)
        )
        _thread, _message, run = await create_thread_message_run(
            session,
            created_at=_CREATED_AT,
            deadline_at=_DEADLINE_AT,
            quota_usage_date=_USAGE_DATE,
            **seed,
        )
    return run.id


async def _read_used_count(session_factory: async_sessionmaker[AsyncSession]) -> int:
    async with session_factory() as session:
        used_count = await session.scalar(
            select(AgentUserDailyQuota.used_count).where(
                AgentUserDailyQuota.user_id == _USER_ID,
                AgentUserDailyQuota.usage_date == _USAGE_DATE,
            )
        )
    assert used_count is not None
    return used_count


async def _read_row(
    session_factory: async_sessionmaker[AsyncSession], run_id: uuid.UUID
) -> _Row:
    async with session_factory() as session:
        row = (
            await session.execute(
                select(
                    AgentRun.status,
                    AgentRun.error_code,
                    AgentRun.attempt_epoch,
                    AgentRun.answer_started_at,
                ).where(AgentRun.id == run_id)
            )
        ).one()
    return row.status, row.error_code, row.attempt_epoch, row.answer_started_at


def _attempt(run_id: uuid.UUID) -> RunAttempt:
    return RunAttempt(run_id=run_id, epoch=1)


def _answer_result() -> AnswerQuestionResult:
    return AnswerQuestionResult(
        status="answered",
        answer="operation conditions answer",
        sources=[],
        missing_aspects=[],
        plan_summary=AnswerPlanSummary(plan_type="direct_answer"),
    )


_ENDED = [
    pytest.param(_FAILED, _JUST_BEFORE_DEADLINE, id="failed"),
    pytest.param(_CANCELLED, _JUST_BEFORE_DEADLINE, id="cancelled"),
    pytest.param(_EXPIRED, _JUST_BEFORE_DEADLINE, id="expired"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("seed", "expected_epoch"),
    [
        pytest.param(_QUEUED, 1, id="queued"),
        pytest.param(_PREPARING, 2, id="preparing"),
    ],
)
async def test_start_takes_over_run_before_answer_until_deadline(
    session_factory: async_sessionmaker[AsyncSession],
    seed: dict[str, Any],
    expected_epoch: int,
) -> None:
    """受理期限の直前なら、待機中のRunも回答前のRunも、開始で実行中になり世代が1つ進む。"""
    run_id = await _seed(session_factory, seed)
    notices = ClosedRunNotices()

    result = await state_service(
        session_factory, notices=notices, now=_JUST_BEFORE_DEADLINE
    ).start_attempt(run_id)

    assert (result, notices.closed_runs, await _read_row(session_factory, run_id)) == (
        AttemptStarted(attempt=RunAttempt(run_id=run_id, epoch=expected_epoch)),
        [],
        ("running", None, expected_epoch, None),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("seed", "now"),
    [
        pytest.param(_QUEUED, _DEADLINE_AT, id="queued-at-deadline"),
        pytest.param(_PREPARING, _DEADLINE_AT, id="preparing-at-deadline"),
        pytest.param(_ANSWERING, _JUST_BEFORE_DEADLINE, id="answering"),
        *_ENDED,
    ],
)
async def test_start_is_refused_without_touching_run(
    session_factory: async_sessionmaker[AsyncSession],
    seed: dict[str, Any],
    now: datetime,
) -> None:
    """受理期限ちょうどのRun・回答中のRun・終わったRunは開始できず、行も通知も変わらない。"""
    run_id = await _seed(session_factory, seed)
    before = await _read_row(session_factory, run_id)
    notices = ClosedRunNotices()

    result = await state_service(
        session_factory, notices=notices, now=now
    ).start_attempt(run_id)

    assert (result, notices.closed_runs, await _read_row(session_factory, run_id)) == (
        CannotContinue(),
        [],
        before,
    )


@pytest.mark.asyncio
async def test_start_is_refused_for_missing_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """行の無いRunは開始できない。"""
    result = await state_service(session_factory).start_attempt(uuid.uuid4())

    assert result == CannotContinue()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("seed", "now", "answer_started_at"),
    [
        pytest.param(_PREPARING, _JUST_BEFORE_DEADLINE, None, id="before-answer"),
        pytest.param(
            _ANSWERING,
            _JUST_BEFORE_RECOVERY_DEADLINE,
            _ANSWER_STARTED_AT,
            id="answering",
        ),
    ],
)
async def test_continuation_is_allowed_until_recovery_deadline(
    session_factory: async_sessionmaker[AsyncSession],
    seed: dict[str, Any],
    now: datetime,
    answer_started_at: datetime | None,
) -> None:
    """回答前は受理期限、回答中は回収期限の直前まで、現在の試行は続けてよく、行は変わらない。"""
    run_id = await _seed(session_factory, seed)
    notices = ClosedRunNotices()

    result = await state_service(
        session_factory, notices=notices, now=now
    ).authorize_continuation(_attempt(run_id))

    assert (result, notices.closed_runs, await _read_row(session_factory, run_id)) == (
        CanContinue(),
        [],
        ("running", None, 1, answer_started_at),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("seed", "now"),
    [
        pytest.param(_PREPARING, _DEADLINE_AT, id="before-answer-at-deadline"),
        pytest.param(_ANSWERING, _RECOVERY_DEADLINE_AT, id="at-recovery-deadline"),
        pytest.param(_TAKEN_OVER, _JUST_BEFORE_DEADLINE, id="taken-over"),
        *_ENDED,
    ],
)
async def test_continuation_is_refused_without_touching_run(
    session_factory: async_sessionmaker[AsyncSession],
    seed: dict[str, Any],
    now: datetime,
) -> None:
    """期限ちょうど・別の試行が担当・終わったRunでは続けられず、行も通知も変わらない。"""
    run_id = await _seed(session_factory, seed)
    before = await _read_row(session_factory, run_id)
    notices = ClosedRunNotices()

    result = await state_service(
        session_factory, notices=notices, now=now
    ).authorize_continuation(_attempt(run_id))

    assert (result, notices.closed_runs, await _read_row(session_factory, run_id)) == (
        CannotContinue(),
        [],
        before,
    )


@pytest.mark.asyncio
async def test_answer_start_is_recorded_before_answer_until_deadline(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """受理期限の直前なら、回答前の現在の試行は回答開始の時刻をその時刻で記録する。"""
    run_id = await _seed(session_factory, _PREPARING)
    notices = ClosedRunNotices()

    result = await state_service(
        session_factory, notices=notices, now=_JUST_BEFORE_DEADLINE
    ).start_answer_generation(_attempt(run_id))

    assert (result, notices.closed_runs, await _read_row(session_factory, run_id)) == (
        AnswerGenerationStarted(run_id=run_id, answer_started_at=_JUST_BEFORE_DEADLINE),
        [],
        ("running", None, 1, _JUST_BEFORE_DEADLINE),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("seed", "now"),
    [
        pytest.param(_PREPARING, _DEADLINE_AT, id="at-deadline"),
        pytest.param(_TAKEN_OVER, _JUST_BEFORE_DEADLINE, id="taken-over"),
        pytest.param(_ANSWERING, _JUST_BEFORE_DEADLINE, id="answering"),
        *_ENDED,
    ],
)
async def test_answer_start_is_refused_without_touching_run(
    session_factory: async_sessionmaker[AsyncSession],
    seed: dict[str, Any],
    now: datetime,
) -> None:
    """受理期限ちょうど・別の試行が担当・既に回答中・終わったRunでは回答を始められず、行も通知も変わらない。"""
    run_id = await _seed(session_factory, seed)
    before = await _read_row(session_factory, run_id)
    notices = ClosedRunNotices()

    result = await state_service(
        session_factory, notices=notices, now=now
    ).start_answer_generation(_attempt(run_id))

    assert (result, notices.closed_runs, await _read_row(session_factory, run_id)) == (
        CannotContinue(),
        [],
        before,
    )


@pytest.mark.asyncio
async def test_completion_is_saved_while_answering_until_recovery_deadline(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """受理期限を過ぎていても回収期限の直前なら、回答中の現在の試行の回答でRunが完了し、その世代へ知らせる。"""
    run_id = await _seed(session_factory, _ANSWERING)
    notices = ClosedRunNotices()

    result = await state_service(
        session_factory, notices=notices, now=_JUST_BEFORE_RECOVERY_DEADLINE
    ).complete(_attempt(run_id), result=_answer_result(), research_handoff=None)

    assert (result, notices.closed_runs, await _read_row(session_factory, run_id)) == (
        RunCompleted(),
        [
            ClosedRunningRun(
                attempt=RunAttempt(run_id=run_id, epoch=1),
                status=AgentRunStatus.COMPLETED,
                error_code=None,
            )
        ],
        ("completed", None, 1, _ANSWER_STARTED_AT),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("seed", "now"),
    [
        pytest.param(_ANSWERING, _RECOVERY_DEADLINE_AT, id="at-recovery-deadline"),
        pytest.param(
            _TAKEN_OVER_ANSWERING, _JUST_BEFORE_RECOVERY_DEADLINE, id="taken-over"
        ),
        pytest.param(_PREPARING, _JUST_BEFORE_DEADLINE, id="before-answer"),
        *_ENDED,
    ],
)
async def test_completion_is_refused_without_touching_run(
    session_factory: async_sessionmaker[AsyncSession],
    seed: dict[str, Any],
    now: datetime,
) -> None:
    """回収期限ちょうど・別の試行が担当・回答前・終わったRunでは回答を保存できず、行も通知も変わらない。"""
    run_id = await _seed(session_factory, seed)
    before = await _read_row(session_factory, run_id)
    notices = ClosedRunNotices()

    result = await state_service(session_factory, notices=notices, now=now).complete(
        _attempt(run_id), result=_answer_result(), research_handoff=None
    )

    assert (result, notices.closed_runs, await _read_row(session_factory, run_id)) == (
        CannotContinue(),
        [],
        before,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("seed", "now", "answer_started_at"),
    [
        pytest.param(_PREPARING, _JUST_BEFORE_DEADLINE, None, id="before-answer"),
        pytest.param(
            _ANSWERING,
            _JUST_BEFORE_RECOVERY_DEADLINE,
            _ANSWER_STARTED_AT,
            id="answering",
        ),
    ],
)
async def test_failure_is_recorded_until_recovery_deadline(
    session_factory: async_sessionmaker[AsyncSession],
    seed: dict[str, Any],
    now: datetime,
    answer_started_at: datetime | None,
) -> None:
    """回答前は受理期限、回答中は回収期限の直前まで、現在の試行の失敗でRunが失敗で終わり、その世代へ知らせる。"""
    run_id = await _seed(session_factory, seed)
    notices = ClosedRunNotices()

    result = await state_service(
        session_factory, notices=notices, now=now
    ).end_run_after_attempt_failure(
        _attempt(run_id), error_code=AgentRunErrorCode.GENERATION_UNAVAILABLE
    )

    assert (result, notices.closed_runs, await _read_row(session_factory, run_id)) == (
        RunFailed(),
        [
            ClosedRunningRun(
                attempt=RunAttempt(run_id=run_id, epoch=1),
                status=AgentRunStatus.FAILED,
                error_code=AgentRunErrorCode.GENERATION_UNAVAILABLE,
            )
        ],
        ("failed", "generation_unavailable", 1, answer_started_at),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("seed", "now"),
    [
        pytest.param(_PREPARING, _DEADLINE_AT, id="before-answer-at-deadline"),
        pytest.param(_ANSWERING, _RECOVERY_DEADLINE_AT, id="at-recovery-deadline"),
        pytest.param(_TAKEN_OVER, _JUST_BEFORE_DEADLINE, id="taken-over"),
        *_ENDED,
    ],
)
async def test_failure_is_refused_without_touching_run(
    session_factory: async_sessionmaker[AsyncSession],
    seed: dict[str, Any],
    now: datetime,
) -> None:
    """期限ちょうど・別の試行が担当・終わったRunでは失敗を記録できず、行も通知も変わらない。"""
    run_id = await _seed(session_factory, seed)
    before = await _read_row(session_factory, run_id)
    notices = ClosedRunNotices()

    result = await state_service(
        session_factory, notices=notices, now=now
    ).end_run_after_attempt_failure(
        _attempt(run_id), error_code=AgentRunErrorCode.GENERATION_UNAVAILABLE
    )

    assert (result, notices.closed_runs, await _read_row(session_factory, run_id)) == (
        CannotContinue(),
        [],
        before,
    )


@pytest.mark.asyncio
async def test_cancel_closes_queued_run_and_releases_quota(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """受理期限の直前なら、待機中のRunは取り消しで終わり、何も通知せず、受付で数えた利用回数が1から0に戻る。"""
    run_id = await _seed(session_factory, _QUEUED)
    notices = ClosedRunNotices()

    result = await state_service(
        session_factory, notices=notices, now=_JUST_BEFORE_DEADLINE
    ).cancel_for_user(run_id, user_id=_USER_ID)

    assert (
        result,
        notices.closed_runs,
        await _read_row(session_factory, run_id),
        await _read_used_count(session_factory),
    ) == (CancelledByUser(), [], ("failed", "cancelled", 0, None), 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("seed", "now", "answer_started_at"),
    [
        pytest.param(_PREPARING, _JUST_BEFORE_DEADLINE, None, id="before-answer"),
        pytest.param(
            _ANSWERING,
            _JUST_BEFORE_RECOVERY_DEADLINE,
            _ANSWER_STARTED_AT,
            id="answering",
        ),
    ],
)
async def test_cancel_closes_running_run_for_its_attempt(
    session_factory: async_sessionmaker[AsyncSession],
    seed: dict[str, Any],
    now: datetime,
    answer_started_at: datetime | None,
) -> None:
    """回答前は受理期限、回答中は回収期限の直前まで、実行中のRunは取り消しで終わり、その世代へ知らせる。"""
    run_id = await _seed(session_factory, seed)
    notices = ClosedRunNotices()

    result = await state_service(
        session_factory, notices=notices, now=now
    ).cancel_for_user(run_id, user_id=_USER_ID)

    assert (result, notices.closed_runs, await _read_row(session_factory, run_id)) == (
        CancelledByUser(),
        [
            ClosedRunningRun(
                attempt=RunAttempt(run_id=run_id, epoch=1),
                status=AgentRunStatus.FAILED,
                error_code=AgentRunErrorCode.CANCELLED,
            )
        ],
        ("failed", "cancelled", 1, answer_started_at),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("seed", "now"),
    [
        pytest.param(_QUEUED, _DEADLINE_AT, id="queued-at-deadline"),
        pytest.param(_PREPARING, _DEADLINE_AT, id="before-answer-at-deadline"),
        pytest.param(_ANSWERING, _RECOVERY_DEADLINE_AT, id="at-recovery-deadline"),
        *_ENDED,
    ],
)
async def test_cancel_is_refused_without_touching_run(
    session_factory: async_sessionmaker[AsyncSession],
    seed: dict[str, Any],
    now: datetime,
) -> None:
    """期限ちょうどのRunと終わったRunは取り消しで書き換わらず、行も通知も変わらない。"""
    run_id = await _seed(session_factory, seed)
    before = await _read_row(session_factory, run_id)
    notices = ClosedRunNotices()

    result = await state_service(
        session_factory, notices=notices, now=now
    ).cancel_for_user(run_id, user_id=_USER_ID)

    assert (result, notices.closed_runs, await _read_row(session_factory, run_id)) == (
        CannotContinue(),
        [],
        before,
    )


@pytest.mark.asyncio
async def test_cancel_by_another_user_is_not_found_without_touching_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """持ち主でない利用者の取り消しは、Runが無いと返り、行も通知も変わらない。"""
    run_id = await _seed(session_factory, _PREPARING)
    notices = ClosedRunNotices()

    result = await state_service(
        session_factory, notices=notices, now=_JUST_BEFORE_DEADLINE
    ).cancel_for_user(run_id, user_id=uuid.uuid4())

    assert (result, notices.closed_runs, await _read_row(session_factory, run_id)) == (
        RunNotFound(),
        [],
        ("running", None, 1, None),
    )


@pytest.mark.asyncio
async def test_cancel_of_missing_run_is_not_found(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """行の無いRunの取り消しは、Runが無いと返る。"""
    result = await state_service(session_factory).cancel_for_user(
        uuid.uuid4(), user_id=_USER_ID
    )

    assert result == RunNotFound()


@pytest.mark.asyncio
async def test_enqueue_failure_closes_queued_run_and_releases_quota(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """受理期限の直前なら、待機中のRunは投入の失敗で終わり、何も通知せず、受付で数えた利用回数が1から0に戻る。"""
    run_id = await _seed(session_factory, _QUEUED)
    notices = ClosedRunNotices()

    result = await state_service(
        session_factory, notices=notices, now=_JUST_BEFORE_DEADLINE
    ).end_run_after_enqueue_failure(run_id)

    assert (
        result,
        notices.closed_runs,
        await _read_row(session_factory, run_id),
        await _read_used_count(session_factory),
    ) == (RunFailed(), [], ("failed", "enqueue_failed", 0, None), 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("seed", "now"),
    [
        pytest.param(_QUEUED, _DEADLINE_AT, id="queued-at-deadline"),
        pytest.param(_PREPARING, _JUST_BEFORE_DEADLINE, id="before-answer"),
        pytest.param(_ANSWERING, _JUST_BEFORE_DEADLINE, id="answering"),
        *_ENDED,
    ],
)
async def test_enqueue_failure_is_refused_without_touching_run(
    session_factory: async_sessionmaker[AsyncSession],
    seed: dict[str, Any],
    now: datetime,
) -> None:
    """受理期限ちょうどの待機中のRun・試行が始まったRun・終わったRunは投入の失敗で書き換わらず、行も通知も変わらない。"""
    run_id = await _seed(session_factory, seed)
    before = await _read_row(session_factory, run_id)
    notices = ClosedRunNotices()

    result = await state_service(
        session_factory, notices=notices, now=now
    ).end_run_after_enqueue_failure(run_id)

    assert (result, notices.closed_runs, await _read_row(session_factory, run_id)) == (
        CannotContinue(),
        [],
        before,
    )
