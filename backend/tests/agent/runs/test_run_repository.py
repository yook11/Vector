"""AgentRunRepository の開始・完了・失敗遷移契約。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

import app.queue.tasks.agent_run as agent_run_tasks
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
from app.agent.runs.contracts import CompleteRunOutcome, StartRunFailureReason
from app.agent.runs.repository import AgentRunRepository
from app.agent.runs.types import AgentRunStatus
from app.models.agent_message import AgentMessage, AgentMessageSource
from app.models.agent_run import AgentRun
from app.models.agent_thread import AgentThread
from app.shared.security.safe_url import SafeUrl
from tests.agent.runs._seed import (
    create_thread_message_run as _create_thread_message_run,
)
from tests.agent.runs._start_run_outcomes import (
    assert_start_failure,
    started_attempt_epoch,
)
from tests.conftest import TEST_ADMIN_ID, TEST_USER_ID


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
                url=SafeUrl("https://example.com/agent-source"),
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


@pytest.mark.asyncio
async def test_read_live_context_for_user_returns_only_owned_internal_fields(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as setup_session:
        _thread, _message, run = await _create_thread_message_run(
            setup_session,
            status="running",
            attempt_epoch=3,
        )

    async with session_factory() as session:
        repo = AgentRunRepository(session)
        owned = await repo.read_live_context_for_user(
            run_id=run.id,
            user_id=UUID(TEST_USER_ID),
        )
        other_user = await repo.read_live_context_for_user(
            run_id=run.id,
            user_id=UUID(TEST_ADMIN_ID),
        )
        missing = await repo.read_live_context_for_user(
            run_id=UUID("00000000-0000-4000-a000-000000000099"),
            user_id=UUID(TEST_USER_ID),
        )

    assert owned is not None
    assert owned.run_id == run.id
    assert owned.status is AgentRunStatus.RUNNING
    assert owned.attempt_epoch == 3
    assert owned.error_code is None
    assert other_user is None
    assert missing is None


@pytest.mark.asyncio
async def test_read_live_context_for_user_preserves_terminal_error_code(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as setup_session:
        _thread, _message, run = await _create_thread_message_run(
            setup_session,
            status="failed",
            attempt_epoch=2,
            error_code="cancelled",
        )

    async with session_factory() as session:
        context = await AgentRunRepository(session).read_live_context_for_user(
            run_id=run.id,
            user_id=UUID(TEST_USER_ID),
        )

    assert context is not None
    assert context.status is AgentRunStatus.FAILED
    assert context.attempt_epoch == 2
    assert context.error_code == "cancelled"


@pytest.mark.asyncio
async def test_complete_run_warns_on_citation_source_mismatch_without_failing_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(
            session,
            status="running",
            answer_started_at=datetime.now(UTC),
        )
    result = AnswerQuestionResult(
        status="answered",
        answer="sensitive answer body [[2]]",
        sources=[
            ExternalUrlSource(
                source_ref="1",
                url=SafeUrl("https://example.com/secret-source-url"),
                title="Sensitive source title",
                evidence_claim="Sensitive evidence claim.",
                source_name="Example",
            )
        ],
        missing_aspects=[],
        plan_summary=_plan_summary("search"),
    )

    with capture_logs() as logs:
        async with session_factory() as session:
            async with session.begin():
                completed = await AgentRunRepository(session).complete_run(
                    run_id=run.id,
                    result=result,
                    expected_attempt_epoch=run.attempt_epoch,
                )

    assert completed is CompleteRunOutcome.COMPLETED
    mismatch_logs = [
        entry
        for entry in logs
        if entry.get("event") == "agent_citation_source_mismatch"
    ]
    assert len(mismatch_logs) == 1
    warning = mismatch_logs[0]
    assert warning["log_level"] == "warning"
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
    """永続化契約: research_handoffへ渡したJSONBは、読み戻して

    ResearchHandoff.model_validateが通る形でagent_threads.research_handoffへ
    そのまま保存される。
    """
    async with session_factory() as session:
        thread, _message, run = await _create_thread_message_run(
            session,
            status="running",
            answer_started_at=datetime.now(UTC),
        )
        thread_id = thread.id
    handoff = _handoff()
    handoff_json = handoff.model_dump(mode="json")

    async with session_factory() as session:
        async with session.begin():
            completed = await AgentRunRepository(session).complete_run(
                run_id=run.id,
                result=_direct_result(),
                expected_attempt_epoch=run.attempt_epoch,
                research_handoff=handoff_json,
            )

    assert completed is CompleteRunOutcome.COMPLETED
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
            answer_started_at=datetime.now(UTC),
        )
        thread_id = thread.id
        thread.research_handoff = stored
        await session.commit()

    async with session_factory() as session:
        async with session.begin():
            completed = await AgentRunRepository(session).complete_run(
                run_id=run.id,
                result=_direct_result(),
                expected_attempt_epoch=run.attempt_epoch,
            )

    assert completed is CompleteRunOutcome.COMPLETED
    async with session_factory() as session:
        persisted_thread = await session.get(AgentThread, thread_id)
        assert persisted_thread is not None
        assert persisted_thread.research_handoff == stored


@pytest.mark.asyncio
async def test_stale_complete_run_with_a_handoff_does_not_persist_it(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """既存guard(attempt_epoch fence)はhandoff導入後も変わらない。

    stale attemptがhandoffを渡しても、epoch fenceに敗れればUPDATEごと
    ロールバックされ、threadのhandoffはNULLのまま残る。
    """
    async with session_factory() as setup_session:
        thread, _message, run = await _create_thread_message_run(
            setup_session,
            status="running",
            attempt_epoch=1,
            answer_started_at=datetime.now(UTC),
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
                    await AgentRunRepository(winner_session).start_run(run.id)
                )
                assert attempt_epoch == 2

        async with stale_session.begin():
            outcome = await AgentRunRepository(stale_session).complete_run(
                run_id=run.id,
                result=_direct_result(),
                expected_attempt_epoch=stale_run.attempt_epoch,
                research_handoff=handoff_json,
            )
        assert outcome is CompleteRunOutcome.TRANSITION_LOST
    finally:
        await stale_session.close()

    async with session_factory() as session:
        current = await session.get(AgentRun, run.id)
        persisted_thread = await session.get(AgentThread, thread_id)
        assert current is not None and persisted_thread is not None
        assert (current.status, current.attempt_epoch) == ("running", 2)
        assert persisted_thread.research_handoff is None


@pytest.mark.asyncio
async def test_complete_run_lost_race_rolls_back_assistant_message(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as setup_session:
        _thread, _message, run = await _create_thread_message_run(
            setup_session,
            status="running",
            answer_started_at=datetime.now(UTC),
        )
    stale_session = session_factory()
    try:
        async with stale_session.begin():
            stale_run = await stale_session.get(AgentRun, run.id)
            assert stale_run is not None and stale_run.status == "running"

        async with session_factory() as winner_session:
            async with winner_session.begin():
                await AgentRunRepository(winner_session).mark_failed(
                    run.id,
                    expected_attempt_epoch=run.attempt_epoch,
                    error_code=agent_run_tasks.AgentRunErrorCode.STALE,
                )

        async with stale_session.begin():
            outcome = await AgentRunRepository(stale_session).complete_run(
                run_id=run.id,
                result=_direct_result(),
                expected_attempt_epoch=stale_run.attempt_epoch,
            )
        assert outcome is CompleteRunOutcome.TRANSITION_LOST
    finally:
        await stale_session.close()

    async with session_factory() as session:
        failed = await session.get(AgentRun, run.id)
        assert failed is not None
        assert failed.status == "failed"
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
async def test_stale_complete_run_loses_epoch_fence_and_rolls_back_artifacts(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
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
            assert stale_run is not None
            assert stale_run.attempt_epoch == 1

        async with session_factory() as winner_session:
            async with winner_session.begin():
                attempt_epoch = started_attempt_epoch(
                    await AgentRunRepository(winner_session).start_run(run.id)
                )
                assert attempt_epoch == 2

        async with stale_session.begin():
            outcome = await AgentRunRepository(stale_session).complete_run(
                run_id=run.id,
                result=_external_result(),
                expected_attempt_epoch=stale_run.attempt_epoch,
            )
        assert outcome is CompleteRunOutcome.TRANSITION_LOST
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
async def test_stale_mark_failed_does_not_alter_newer_attempt(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as setup_session:
        _thread, _message, run = await _create_thread_message_run(
            setup_session,
            status="running",
            attempt_epoch=1,
        )

    async with session_factory() as session:
        async with session.begin():
            attempt_epoch = started_attempt_epoch(
                await AgentRunRepository(session).start_run(run.id)
            )
            transitioned = await AgentRunRepository(session).mark_failed(
                run.id,
                expected_attempt_epoch=1,
                error_code=agent_run_tasks.AgentRunErrorCode.STALE,
            )

    assert transitioned is False
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
            repo = AgentRunRepository(session)
            attempt_epoch = started_attempt_epoch(
                await repo.start_run(running.id, now=now)
            )
            skipped = await repo.start_run(failed.id, now=now)

    assert attempt_epoch == 2
    async with session_factory() as session:
        loaded = await AgentRunRepository(session).read_user_question_for_run(
            running.id
        )
    assert loaded is not None
    _user_id, thread_id, question = loaded
    assert thread_id == running.thread_id
    assert question.content == "worker question"
    assert question.seq == 1
    assert_start_failure(skipped, StartRunFailureReason.ALREADY_FINISHED)
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
async def test_start_run_allocates_first_attempt_epoch(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as setup_session:
        _thread, _message, run = await _create_thread_message_run(setup_session)

    async with session_factory() as session:
        async with session.begin():
            attempt_epoch = started_attempt_epoch(
                await AgentRunRepository(session).start_run(run.id)
            )

    async with session_factory() as session:
        started_run = await session.get(AgentRun, run.id)
        assert started_run is not None
        assert attempt_epoch == 1
        assert started_run.attempt_epoch == 1


@pytest.mark.asyncio
async def test_start_run_increment_rolls_back_with_transaction(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as setup_session:
        _thread, _message, run = await _create_thread_message_run(setup_session)

    async with session_factory() as session:
        attempt_epoch = started_attempt_epoch(
            await AgentRunRepository(session).start_run(run.id)
        )
        assert attempt_epoch == 1
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
    async with session_factory() as setup_session:
        _thread, _message, run = await _create_thread_message_run(setup_session)

    async def start_once() -> int:
        async with session_factory() as session:
            async with session.begin():
                return started_attempt_epoch(
                    await AgentRunRepository(session).start_run(run.id)
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
    missing_run_id = UUID("00000000-0000-4000-a000-000000000099")

    async with session_factory() as session:
        async with session.begin():
            skip_result = await AgentRunRepository(session).start_run(missing_run_id)

    assert_start_failure(skip_result, StartRunFailureReason.RUN_NOT_FOUND)


@pytest.mark.asyncio
async def test_start_run_reports_idempotent_skip_when_transition_loses_race(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as setup_session:
        _thread, _message, run = await _create_thread_message_run(setup_session)

    selected = asyncio.Event()
    contender = session_factory()
    winner = session_factory()
    original_execute = contender.execute

    async def execute_with_pause(*args: object, **kwargs: object) -> object:
        selected.set()
        return await original_execute(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(contender, "execute", execute_with_pause)
    try:

        async def start_once() -> object:
            async with contender.begin():
                return await AgentRunRepository(contender).start_run(run.id)

        await winner.begin()
        changed = await AgentRunRepository(winner).mark_failed(
            run.id,
            expected_attempt_epoch=run.attempt_epoch,
            error_code=agent_run_tasks.AgentRunErrorCode.STALE,
        )
        assert changed is True
        start_task = asyncio.create_task(start_once())
        await asyncio.wait_for(selected.wait(), timeout=1)
        await winner.commit()
        skip_result = await start_task
    finally:
        await winner.close()
        await contender.close()

    assert_start_failure(skip_result, StartRunFailureReason.ALREADY_FINISHED)
    async with session_factory() as session:
        failed = await session.get(AgentRun, run.id)
        assert failed is not None
        assert failed.status == "failed"
        assert failed.attempt_epoch == 0


@pytest.mark.asyncio
async def test_mark_enqueue_failed_remains_epoch_independent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as setup_session:
        _thread, _message, run = await _create_thread_message_run(
            setup_session,
            attempt_epoch=7,
        )

    async with session_factory() as session:
        async with session.begin():
            transitioned = await AgentRunRepository(session).mark_enqueue_failed(
                run.id,
            )

    assert transitioned is True
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
    from sqlalchemy import func, text
    from sqlalchemy.exc import DBAPIError

    monkeypatch.setattr("app.agent.runs.repository._ANSWER_SAVE_LOCK_TIMEOUT", "30ms")
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
                        AgentRunRepository(saver).complete_run(
                            run_id=run.id,
                            result=_external_result(),
                            expected_attempt_epoch=run.attempt_epoch,
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
    from sqlalchemy import func

    async with session_factory() as session:
        _, _, run = await _create_thread_message_run(
            session,
            status="running",
            answer_started_at=datetime.now(UTC),
        )
        run_id, epoch = run.id, run.attempt_epoch
        before = await session.scalar(select(func.current_setting("lock_timeout")))
        await session.rollback()
        async with session.begin():
            outcome = await AgentRunRepository(session).complete_run(
                run_id=run_id,
                result=_direct_result(),
                expected_attempt_epoch=epoch,
            )
            assert (
                await session.scalar(select(func.current_setting("lock_timeout")))
                == "3s"
            )
        assert outcome is CompleteRunOutcome.COMPLETED
        assert (
            await session.scalar(select(func.current_setting("lock_timeout"))) == before
        )
