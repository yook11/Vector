"""AgentRunStateRepository の開始・完了・失敗記録・受付失敗を実DBで確かめる。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.contract import (
    AnswerPlanSummary,
    AnswerQuestionResult,
    ExternalUrlSource,
)
from app.agent.research_handoff import (
    ResearchHandoff,
    ResearchRunRecord,
    ResearchTaskRecord,
)
from app.agent.running.daily_quota.release import DailyQuotaReleaseOutcome
from app.agent.running.finish_notice import ClosedQueuedRun, ClosedRunningRun
from app.agent.running.question_reading import AgentRunQuestionRepository
from app.agent.running.state import (
    AttemptStarted,
    AttemptSuperseded,
    CancelledByUser,
    CannotContinue,
    FailureRecorded,
    RunCompleted,
    RunNotFound,
    RunPhaseMismatchError,
)
from app.agent.running.state_repository import AgentRunStateRepository
from app.agent.running.types import AgentRunErrorCode, AgentRunStatus
from app.models.agent_message import AgentMessage, AgentMessageSource
from app.models.agent_run import AgentRun
from app.models.agent_thread import AgentThread
from app.shared.web_url import WebUrl
from tests.agent.running._seed import (
    create_thread_message_run as _create_thread_message_run,
)
from tests.agent.running._start_run_outcomes import started_attempt_epoch
from tests.conftest import TEST_USER_ID


def _plan_summary(plan_type: str) -> AnswerPlanSummary:
    return AnswerPlanSummary(plan_type=plan_type)


def _direct_result(answer: str = "worker answer") -> AnswerQuestionResult:
    return AnswerQuestionResult(
        status="answered",
        answer=answer,
        sources=[],
        missing_aspects=[],
        plan_summary=_plan_summary("direct_answer"),
    )


def _external_result() -> AnswerQuestionResult:
    return AnswerQuestionResult(
        status="answered",
        answer="外部根拠つき回答。[[1]]",
        sources=[
            ExternalUrlSource(
                source_ref="1",
                url=WebUrl("https://example.com/agent-source"),
                title="Agent source",
                evidence_claim="Agent claim.",
                source_name="Example",
            )
        ],
        missing_aspects=[],
        plan_summary=_plan_summary("search"),
    )


def _handoff() -> ResearchHandoff:
    """1 Run分の台帳と、整理を書き終えた handoff。"""
    as_of = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)
    return ResearchHandoff(
        updated_at=as_of,
        runs=(
            ResearchRunRecord(
                as_of=as_of,
                tasks=(
                    ResearchTaskRecord(
                        research_goal="調査目標",
                        executed_queries=("q-a",),
                    ),
                ),
            ),
        ),
        collected_overview="供給網の記事が集まった",
        unresolved_points="在庫水準は確認できていない",
        next_search_guidance="決算資料をあたる",
    )


async def _wait_until_blocked(observer: AsyncSession, backend_pid: int) -> None:
    async with asyncio.timeout(5):
        while True:
            await observer.execute(text("SELECT pg_stat_clear_snapshot()"))
            is_waiting_for_lock = await observer.scalar(
                text(
                    """
                    SELECT wait_event_type = 'Lock'
                           AND cardinality(pg_blocking_pids(pid)) > 0
                    FROM pg_stat_activity
                    WHERE pid = :pid
                    """
                ),
                {"pid": backend_pid},
            )
            if is_waiting_for_lock:
                return
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_complete_run_warns_on_citation_source_mismatch_without_failing_run(
    read_logs,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """出典と引用番号が食い違う回答も完了し、本文や出典を含まない警告だけを残す。"""
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(
            session,
            status="running",
            attempt_epoch=1,
            answer_started_at=datetime.now(UTC),
        )
    result = AnswerQuestionResult(
        status="answered",
        answer="sensitive answer body [[2]]",
        sources=[
            ExternalUrlSource(
                source_ref="1",
                url=WebUrl("https://example.com/secret-source-url"),
                title="Sensitive source title",
                evidence_claim="Sensitive evidence claim.",
                source_name="Example",
            )
        ],
        missing_aspects=[],
        plan_summary=_plan_summary("search"),
    )

    async with session_factory() as session:
        async with session.begin():
            repository = AgentRunStateRepository(session)
            completed = await repository.complete(
                run.id,
                attempt_epoch=run.attempt_epoch,
                result=result,
            )
    logs = read_logs()

    assert (completed, repository.closed_runs) == (
        RunCompleted(),
        [
            ClosedRunningRun(
                run_id=run.id,
                attempt_epoch=1,
                status=AgentRunStatus.COMPLETED,
                error_code=None,
            )
        ],
    )
    mismatch_logs = [
        entry
        for entry in logs
        if entry.get("event") == "agent_citation_source_mismatch"
    ]
    assert len(mismatch_logs) == 1
    warning = mismatch_logs[0]
    assert warning["level"] == "warning"
    assert warning["run_id"] == str(run.id)
    assert warning["marker_without_source_refs"] == ["2"]
    assert warning["source_without_marker_refs"] == ["1"]
    serialized_warning = repr(warning)
    assert "sensitive answer body" not in serialized_warning
    assert "secret-source-url" not in serialized_warning
    assert "Sensitive source title" not in serialized_warning
    assert "Sensitive evidence claim" not in serialized_warning

    async with session_factory() as session:
        completed_run = await session.get(AgentRun, run.id)
        assert completed_run is not None
        assert completed_run.status == "completed"
        assert completed_run.assistant_message_id is not None


@pytest.mark.asyncio
async def test_complete_run_persists_the_handoff_on_the_thread_and_round_trips(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """完了で渡したhandoffのJSONBはthreadへそのまま保存され、読み戻して検証できる。"""
    async with session_factory() as session:
        thread, _message, run = await _create_thread_message_run(
            session,
            status="running",
            attempt_epoch=1,
            answer_started_at=datetime.now(UTC),
        )
        thread_id = thread.id
    handoff = _handoff()
    handoff_json = handoff.model_dump(mode="json")

    async with session_factory() as session:
        async with session.begin():
            repository = AgentRunStateRepository(session)
            completed = await repository.complete(
                run.id,
                attempt_epoch=run.attempt_epoch,
                result=_direct_result(),
                research_handoff=handoff_json,
            )

    assert (completed, repository.closed_runs) == (
        RunCompleted(),
        [
            ClosedRunningRun(
                run_id=run.id,
                attempt_epoch=1,
                status=AgentRunStatus.COMPLETED,
                error_code=None,
            )
        ],
    )
    async with session_factory() as session:
        persisted_run = await session.get(AgentRun, run.id)
        persisted_thread = await session.get(AgentThread, thread_id)
        assert persisted_run is not None and persisted_thread is not None
        assert persisted_run.status == "completed"
        assert persisted_thread.research_handoff == handoff_json
        round_tripped = ResearchHandoff.model_validate(
            persisted_thread.research_handoff
        )
        assert round_tripped == handoff


@pytest.mark.asyncio
async def test_complete_run_keeps_the_existing_handoff_when_none_is_passed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Noneは「記録を追加しなかった」であり、threadの既存handoffを消さない。"""
    stored = _handoff().model_dump(mode="json")
    async with session_factory() as session:
        thread, _message, run = await _create_thread_message_run(
            session,
            status="running",
            attempt_epoch=1,
            answer_started_at=datetime.now(UTC),
        )
        thread_id = thread.id
        thread.research_handoff = stored
        await session.commit()

    async with session_factory() as session:
        async with session.begin():
            repository = AgentRunStateRepository(session)
            completed = await repository.complete(
                run.id,
                attempt_epoch=run.attempt_epoch,
                result=_direct_result(),
            )

    assert (completed, repository.closed_runs) == (
        RunCompleted(),
        [
            ClosedRunningRun(
                run_id=run.id,
                attempt_epoch=1,
                status=AgentRunStatus.COMPLETED,
                error_code=None,
            )
        ],
    )
    async with session_factory() as session:
        persisted_thread = await session.get(AgentThread, thread_id)
        assert persisted_thread is not None
        assert persisted_thread.research_handoff == stored


@pytest.mark.asyncio
async def test_stale_complete_run_with_a_handoff_does_not_persist_it(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """古い世代の完了はhandoffを渡しても書き込む前に拒否され、threadのhandoffはNULLのまま残る。"""
    async with session_factory() as setup_session:
        thread, _message, run = await _create_thread_message_run(
            setup_session,
            status="running",
            attempt_epoch=1,
        )
        thread_id = thread.id
    handoff_json = _handoff().model_dump(mode="json")
    stale_session = session_factory()
    try:
        async with stale_session.begin():
            stale_run = await stale_session.get(AgentRun, run.id)
            assert stale_run is not None
            assert stale_run.attempt_epoch == 1

        async with session_factory() as winner_session:
            async with winner_session.begin():
                attempt_epoch = started_attempt_epoch(
                    await AgentRunStateRepository(winner_session).start_attempt(run.id)
                )
                assert attempt_epoch == 2

        async with session_factory() as answer_session:
            async with answer_session.begin():
                await AgentRunStateRepository(answer_session).start_answer_generation(
                    run.id, attempt_epoch=attempt_epoch
                )

        async with stale_session.begin():
            outcome = await AgentRunStateRepository(stale_session).complete(
                run.id,
                attempt_epoch=stale_run.attempt_epoch,
                result=_direct_result(),
                research_handoff=handoff_json,
            )
        assert outcome == AttemptSuperseded()
    finally:
        await stale_session.close()

    async with session_factory() as session:
        current = await session.get(AgentRun, run.id)
        persisted_thread = await session.get(AgentThread, thread_id)
        assert current is not None and persisted_thread is not None
        assert (current.status, current.attempt_epoch) == ("running", 2)
        assert persisted_thread.research_handoff is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cancelled", "expected"),
    [
        pytest.param(False, AttemptSuperseded(), id="failure-recorded"),
        pytest.param(True, CancelledByUser(), id="cancelled"),
    ],
)
async def test_complete_run_lost_race_leaves_no_assistant_message(
    session_factory: async_sessionmaker[AsyncSession],
    cancelled: bool,
    expected: CannotContinue,
) -> None:
    """失敗記録かキャンセルに先を越された完了は、先に確定した終わり方を返して回答を残さない。"""
    async with session_factory() as setup_session:
        _thread, _message, run = await _create_thread_message_run(
            setup_session,
            status="running",
            attempt_epoch=1,
            answer_started_at=datetime.now(UTC),
        )
    stale_session = session_factory()
    try:
        async with stale_session.begin():
            stale_run = await stale_session.get(AgentRun, run.id)
            assert stale_run is not None and stale_run.status == "running"

        async with session_factory() as winner_session:
            async with winner_session.begin():
                if cancelled:
                    await AgentRunStateRepository(winner_session).cancel_for_user(
                        run.id, user_id=UUID(TEST_USER_ID)
                    )
                else:
                    await AgentRunStateRepository(winner_session).record_failure(
                        run.id,
                        attempt_epoch=run.attempt_epoch,
                        error_code=AgentRunErrorCode.STALE,
                    )

        async with stale_session.begin():
            outcome = await AgentRunStateRepository(stale_session).complete(
                run.id,
                attempt_epoch=stale_run.attempt_epoch,
                result=_direct_result(),
            )
        assert outcome == expected
    finally:
        await stale_session.close()

    async with session_factory() as session:
        failed = await session.get(AgentRun, run.id)
        assert failed is not None
        assert failed.status == "failed"
        if cancelled:
            assert failed.error_code == "cancelled"
        assert failed.assistant_message_id is None
        messages = (
            (
                await session.execute(
                    select(AgentMessage).where(
                        AgentMessage.thread_id == failed.thread_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [message.role for message in messages] == ["user"]


@pytest.mark.asyncio
async def test_stale_complete_run_loses_epoch_fence_and_leaves_no_artifacts(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """古い世代の完了は新しい世代を変えず、回答と出典を書かない。"""
    async with session_factory() as setup_session:
        _thread, _message, run = await _create_thread_message_run(
            setup_session,
            status="running",
            attempt_epoch=1,
        )
    stale_session = session_factory()
    try:
        async with stale_session.begin():
            stale_run = await stale_session.get(AgentRun, run.id)
            assert stale_run is not None
            assert stale_run.attempt_epoch == 1

        async with session_factory() as winner_session:
            async with winner_session.begin():
                attempt_epoch = started_attempt_epoch(
                    await AgentRunStateRepository(winner_session).start_attempt(run.id)
                )
                assert attempt_epoch == 2

        async with session_factory() as answer_session:
            async with answer_session.begin():
                await AgentRunStateRepository(answer_session).start_answer_generation(
                    run.id, attempt_epoch=attempt_epoch
                )

        async with stale_session.begin():
            outcome = await AgentRunStateRepository(stale_session).complete(
                run.id,
                attempt_epoch=stale_run.attempt_epoch,
                result=_external_result(),
            )
        assert outcome == AttemptSuperseded()
    finally:
        await stale_session.close()

    async with session_factory() as session:
        current = await session.get(AgentRun, run.id)
        assert current is not None
        current_state = (
            current.status,
            current.attempt_epoch,
            current.assistant_message_id,
        )
        assert current_state == (
            "running",
            2,
            None,
        )
        messages = (
            (
                await session.execute(
                    select(AgentMessage).where(
                        AgentMessage.thread_id == current.thread_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [m.role for m in messages] == ["user"]
        source_rows = (
            (
                await session.execute(
                    select(AgentMessageSource)
                    .join(
                        AgentMessage,
                        AgentMessageSource.message_id == AgentMessage.id,
                    )
                    .where(AgentMessage.thread_id == current.thread_id)
                )
            )
            .scalars()
            .all()
        )
        assert source_rows == []


@pytest.mark.asyncio
async def test_stale_record_failure_does_not_alter_newer_attempt(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """古い世代の失敗記録は拒否され、新しい世代のRunを変えない。"""
    async with session_factory() as setup_session:
        _thread, _message, run = await _create_thread_message_run(
            setup_session,
            status="running",
            attempt_epoch=1,
        )

    async with session_factory() as session:
        async with session.begin():
            repository = AgentRunStateRepository(session)
            attempt_epoch = started_attempt_epoch(
                await repository.start_attempt(run.id)
            )
            recorded = await repository.record_failure(
                run.id,
                attempt_epoch=1,
                error_code=AgentRunErrorCode.STALE,
            )

    assert recorded == AttemptSuperseded()
    async with session_factory() as session:
        current = await session.get(AgentRun, run.id)
        assert current is not None
        assert (current.status, current.attempt_epoch, current.error_code) == (
            "running",
            attempt_epoch,
            None,
        )


@pytest.mark.asyncio
async def test_start_run_reexecutes_running_and_skips_terminal_runs(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """running の再配信は次の世代で開始し、終了済みのRunは開始しない。"""
    now = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)
    async with session_factory() as setup_session:
        _thread, _message, running = await _create_thread_message_run(
            setup_session,
            status="running",
            attempt_epoch=1,
        )
        _terminal_thread, _terminal_message, failed = await _create_thread_message_run(
            setup_session,
            status="failed",
            error_code="internal_error",
        )

    async with session_factory() as session:
        async with session.begin():
            repo = AgentRunStateRepository(session)
            attempt_epoch = started_attempt_epoch(
                await repo.start_attempt(running.id, now=now)
            )
            skipped = await repo.start_attempt(failed.id, now=now)

    assert attempt_epoch == 2
    async with session_factory() as session:
        loaded = await AgentRunQuestionRepository(session).read_user_question_for_run(
            running.id
        )
    assert loaded is not None
    _user_id, thread_id, question = loaded
    assert thread_id == running.thread_id
    assert question.content == "worker question"
    assert question.seq == 1
    assert skipped == AttemptSuperseded()
    async with session_factory() as session:
        restarted = await session.get(AgentRun, running.id)
        terminal = await session.get(AgentRun, failed.id)
        assert restarted is not None
        assert terminal is not None
        assert restarted.status == "running"
        assert restarted.attempt_epoch == 2
        assert terminal.status == "failed"
        assert terminal.error_code == "internal_error"
        assert terminal.attempt_epoch == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "redelivery_delay",
    [
        pytest.param(timedelta(seconds=4), id="before-original-deadline"),
        pytest.param(timedelta(seconds=5), id="at-original-deadline"),
        pytest.param(timedelta(seconds=6), id="after-original-deadline"),
    ],
)
async def test_start_run_rejects_answering_run_without_changing_attempt(
    session_factory: async_sessionmaker[AsyncSession],
    redelivery_delay: timedelta,
) -> None:
    """回答生成開始済みなら別の試行が担当しているとして開始せず、世代と状態を変えない。"""
    created_at = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
    answer_started_at = created_at + timedelta(seconds=55)
    redelivered_at = answer_started_at + redelivery_delay
    initial_epoch = 1
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(
            session,
            created_at=created_at,
            status="running",
            attempt_epoch=initial_epoch,
            answer_started_at=answer_started_at,
        )

    async with session_factory() as session:
        async with session.begin():
            redelivery = await AgentRunStateRepository(session).start_attempt(
                run.id, now=redelivered_at
            )

    assert redelivery == AttemptSuperseded()
    async with session_factory() as session:
        current = await session.get(AgentRun, run.id)
        assert current is not None
        assert (current.status, current.attempt_epoch, current.answer_started_at) == (
            "running",
            initial_epoch,
            answer_started_at,
        )


@pytest.mark.asyncio
async def test_start_run_allocates_first_attempt_epoch(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """queued のRunの最初の開始は世代1を割り当てる。"""
    async with session_factory() as setup_session:
        _thread, _message, run = await _create_thread_message_run(setup_session)

    async with session_factory() as session:
        async with session.begin():
            started = await AgentRunStateRepository(session).start_attempt(run.id)

    async with session_factory() as session:
        started_run = await session.get(AgentRun, run.id)
        assert started_run is not None
        assert started == AttemptStarted(attempt_epoch=1)
        assert started_run.attempt_epoch == 1


@pytest.mark.asyncio
async def test_start_run_increment_rolls_back_with_transaction(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """開始の書き込みは呼び出し元のtransactionと一緒に巻き戻る。"""
    async with session_factory() as setup_session:
        _thread, _message, run = await _create_thread_message_run(setup_session)

    async with session_factory() as session:
        started = await AgentRunStateRepository(session).start_attempt(run.id)
        assert started == AttemptStarted(attempt_epoch=1)
        await session.rollback()

    async with session_factory() as session:
        unchanged = await session.get(AgentRun, run.id)
        assert unchanged is not None
        assert unchanged.status == "queued"
        assert unchanged.attempt_epoch == 0


@pytest.mark.asyncio
async def test_concurrent_start_runs_receive_distinct_sequence_values(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """同時に開始した2つのworkerは行ロックで順に並び、別々の世代を受け取る。"""
    async with session_factory() as setup_session:
        _thread, _message, run = await _create_thread_message_run(setup_session)

    async def start_once() -> int:
        async with session_factory() as session:
            async with session.begin():
                return started_attempt_epoch(
                    await AgentRunStateRepository(session).start_attempt(run.id)
                )

    epochs = await asyncio.gather(start_once(), start_once())

    assert sorted(epochs) == [1, 2]
    async with session_factory() as session:
        started_run = await session.get(AgentRun, run.id)
        assert started_run is not None
        assert started_run.attempt_epoch == 2


@pytest.mark.asyncio
async def test_start_run_reports_idempotent_skip_for_missing_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """存在しないRunの開始は見つからないとして拒否する。"""
    missing_run_id = UUID("00000000-0000-4000-a000-000000000099")

    async with session_factory() as session:
        async with session.begin():
            skip_result = await AgentRunStateRepository(session).start_attempt(
                missing_run_id
            )

    assert skip_result == RunNotFound()


@pytest.mark.asyncio
async def test_start_run_reports_idempotent_skip_when_transition_loses_race(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """行ロックを待った開始は、先に確定したキャンセルを見て世代を進めずに拒否する。"""
    async with session_factory() as setup_session:
        _thread, _message, run = await _create_thread_message_run(setup_session)

    async with (
        session_factory() as winner,
        session_factory() as contender,
        session_factory() as observer,
    ):
        start_task: asyncio.Task[AttemptStarted | CannotContinue] | None = None
        try:
            await winner.begin()
            winner_repository = AgentRunStateRepository(winner)
            cancelled = await winner_repository.cancel_for_user(
                run.id, user_id=UUID(TEST_USER_ID)
            )

            await contender.begin()
            contender_pid = await contender.scalar(text("SELECT pg_backend_pid()"))
            assert isinstance(contender_pid, int)
            start_task = asyncio.create_task(
                AgentRunStateRepository(contender).start_attempt(run.id)
            )
            await _wait_until_blocked(observer, contender_pid)

            await winner.commit()
            skip_result = await asyncio.wait_for(start_task, timeout=5)
            await contender.commit()
        finally:
            if start_task is not None:
                if not start_task.done():
                    start_task.cancel()
                await asyncio.gather(start_task, return_exceptions=True)
            for session in (winner, contender, observer):
                if session.in_transaction():
                    await session.rollback()

    assert (cancelled, winner_repository.closed_runs) == (
        CancelledByUser(),
        [
            ClosedQueuedRun(
                run_id=run.id,
                quota_release_outcome=DailyQuotaReleaseOutcome.NOT_ELIGIBLE,
            )
        ],
    )
    assert skip_result == CancelledByUser()
    async with session_factory() as session:
        failed = await session.get(AgentRun, run.id)
        assert failed is not None
        assert failed.status == "failed"
        assert failed.attempt_epoch == 0


@pytest.mark.asyncio
async def test_record_enqueue_failure_remains_epoch_independent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """受付失敗は世代を見ずにqueuedのRunを失敗で終え、世代を変えない。"""
    async with session_factory() as setup_session:
        _thread, _message, run = await _create_thread_message_run(
            setup_session,
            attempt_epoch=7,
        )

    async with session_factory() as session:
        async with session.begin():
            repository = AgentRunStateRepository(session)
            recorded = await repository.record_enqueue_failure(run.id)

    assert (recorded, repository.closed_runs) == (
        FailureRecorded(),
        [
            ClosedQueuedRun(
                run_id=run.id,
                quota_release_outcome=DailyQuotaReleaseOutcome.NOT_ELIGIBLE,
            )
        ],
    )
    async with session_factory() as session:
        failed = await session.get(AgentRun, run.id)
        assert failed is not None
        assert failed.status == "failed"
        assert failed.error_code == "enqueue_failed"
        assert failed.attempt_epoch == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("locked_model", [AgentRun, AgentThread, AgentMessageSource])
async def test_answer_save_lock_timeout_rolls_back_without_artifacts(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    locked_model: type[AgentRun] | type[AgentThread] | type[AgentMessageSource],
) -> None:
    """完了がロック待ちで時間切れになると、Run・回答・出典・handoffを何も残さない。"""
    from sqlalchemy import func, text
    from sqlalchemy.exc import DBAPIError

    monkeypatch.setattr("app.agent.running.state_repository._LOCK_TIMEOUT", "30ms")
    async with session_factory() as session:
        thread, _, run = await _create_thread_message_run(
            session,
            status="running",
            answer_started_at=datetime.now(UTC),
        )
    locked_id = run.id if locked_model is AgentRun else thread.id

    async with session_factory() as blocker, session_factory() as saver:
        before = await saver.scalar(select(func.current_setting("lock_timeout")))
        await saver.rollback()
        async with blocker.begin():
            if locked_model is AgentMessageSource:
                await blocker.execute(
                    text("LOCK TABLE agent_message_sources IN SHARE MODE")
                )
            else:
                await blocker.execute(
                    select(locked_model)
                    .where(locked_model.id == locked_id)
                    .with_for_update()
                )
            with pytest.raises(DBAPIError) as raised:
                async with saver.begin():
                    await asyncio.wait_for(
                        AgentRunStateRepository(saver).complete(
                            run.id,
                            attempt_epoch=run.attempt_epoch,
                            result=_external_result(),
                            research_handoff=_handoff().model_dump(mode="json"),
                        ),
                        timeout=2,
                    )
            assert raised.value.orig.sqlstate == "55P03"
        assert (
            await saver.scalar(select(func.current_setting("lock_timeout"))) == before
        )
        persisted = await saver.get(AgentRun, run.id)
        assert persisted.status == "running"
        assert persisted.assistant_message_id is None
        messages = (
            await saver.scalars(
                select(AgentMessage).where(AgentMessage.thread_id == thread.id)
            )
        ).all()
        assert [message.role for message in messages] == ["user"]
        assert (
            await saver.scalar(select(func.count()).select_from(AgentMessageSource))
            == 0
        )
        saved_thread = await saver.get(AgentThread, thread.id)
        assert saved_thread.research_handoff == thread.research_handoff


@pytest.mark.asyncio
async def test_answer_save_lock_timeout_is_transaction_local_after_commit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """完了が設定するlock_timeoutはそのtransactionの中だけで効き、commit後は元に戻る。"""
    from sqlalchemy import func

    async with session_factory() as session:
        _, _, run = await _create_thread_message_run(
            session,
            status="running",
            attempt_epoch=1,
            answer_started_at=datetime.now(UTC),
        )
        run_id, epoch = run.id, run.attempt_epoch
        before = await session.scalar(select(func.current_setting("lock_timeout")))
        await session.rollback()
        async with session.begin():
            repository = AgentRunStateRepository(session)
            outcome = await repository.complete(
                run_id,
                attempt_epoch=epoch,
                result=_direct_result(),
            )
            assert (
                await session.scalar(select(func.current_setting("lock_timeout")))
                == "3s"
            )
        assert (outcome, repository.closed_runs) == (
            RunCompleted(),
            [
                ClosedRunningRun(
                    run_id=run_id,
                    attempt_epoch=1,
                    status=AgentRunStatus.COMPLETED,
                    error_code=None,
                )
            ],
        )
        assert (
            await session.scalar(select(func.current_setting("lock_timeout"))) == before
        )


@pytest.mark.parametrize(
    ("status", "attempt_epoch", "answer_started_at"),
    [
        ("queued", 2, None),
        ("running", 2, None),
        ("running", 2, datetime(2026, 1, 1, tzinfo=UTC)),
    ],
)
@pytest.mark.asyncio
async def test_completion_failure_reason_precedence_preserves_existing_data(
    status: str,
    attempt_epoch: int,
    answer_started_at: datetime | None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """世代の違う完了は期限より先に別の試行の担当と判定され、Run・thread・既存の回答を変えない。"""
    async with session_factory() as session:
        thread, _, run = await _create_thread_message_run(
            session,
            status=status,
            attempt_epoch=attempt_epoch,
            answer_started_at=answer_started_at,
            history=[("assistant", "既存の回答")],
        )
        run_id, thread_id = run.id, thread.id
        original_updated_at = thread.updated_at
    async with session_factory() as session:
        async with session.begin():
            completion = await AgentRunStateRepository(session).complete(
                run_id,
                attempt_epoch=1,
                result=_external_result(),
                research_handoff=_handoff().model_dump(mode="json"),
            )
            assert completion == AttemptSuperseded()
            # 事前拒否が書き込まないことも確認するため、このテストではcommitする。
    async with session_factory() as session:
        persisted = await session.get(AgentRun, run_id)
        saved_thread = await session.get(AgentThread, thread_id)
        assert persisted is not None and saved_thread is not None
        assert (
            persisted.status,
            persisted.attempt_epoch,
            persisted.answer_started_at,
        ) == (status, attempt_epoch, answer_started_at)
        assert persisted.assistant_message_id is None
        assert saved_thread.research_handoff is None
        assert saved_thread.updated_at == original_updated_at
        assert list(
            await session.execute(
                select(AgentMessage.role, AgentMessage.content)
                .where(AgentMessage.thread_id == thread_id)
                .order_by(AgentMessage.seq)
            )
        ) == [("assistant", "既存の回答"), ("user", "worker question")]
        assert list(await session.scalars(select(AgentMessageSource))) == []


@pytest.mark.asyncio
async def test_completion_before_answer_start_is_a_phase_mismatch_without_writes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """回答を始めていない現在の試行の完了は呼び出し順の誤りとして例外になり、Run・threadを変えない。"""
    async with session_factory() as session:
        thread, _, run = await _create_thread_message_run(
            session,
            status="running",
            attempt_epoch=1,
            history=[("assistant", "既存の回答")],
        )
        run_id, thread_id = run.id, thread.id
        original_updated_at = thread.updated_at

    async with session_factory() as session:
        with pytest.raises(RunPhaseMismatchError):
            async with session.begin():
                await AgentRunStateRepository(session).complete(
                    run_id,
                    attempt_epoch=1,
                    result=_external_result(),
                    research_handoff=_handoff().model_dump(mode="json"),
                )

    async with session_factory() as session:
        persisted = await session.get(AgentRun, run_id)
        saved_thread = await session.get(AgentThread, thread_id)
        assert persisted is not None and saved_thread is not None
        assert (
            persisted.status,
            persisted.attempt_epoch,
            persisted.answer_started_at,
            persisted.assistant_message_id,
            saved_thread.research_handoff,
            saved_thread.updated_at,
        ) == ("running", 1, None, None, None, original_updated_at)
