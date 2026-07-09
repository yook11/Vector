"""Agent run worker and history repository tests."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

import app.queue.tasks.agent_run as agent_run_tasks
from app.agent.contract import (
    AnswerQuestionInput,
    AnswerQuestionResult,
    AnswerRetrievalSummary,
    ExternalUrlSource,
    InternalArticleSource,
)
from app.agent.history import AgentHistoryRepository, RunTransitionLostError
from app.agent.history.mapper import (
    build_assistant_message_for_result,
    build_source_rows_for_message,
)
from app.agent.history.projection import build_research_assistant_message
from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderError,
)
from app.agent.question_resolution.contract import ResolvedQuestionDraft
from app.models.agent_message import AgentMessage, AgentMessageSource
from app.models.agent_run import AgentRun
from app.models.agent_thread import AgentThread
from app.queue.messages.agent_run import AgentRunTrigger
from app.shared.security.safe_url import SafeUrl
from tests.conftest import TEST_USER_ID


class FakeAgent:
    def __init__(
        self,
        result: AnswerQuestionResult | None = None,
        exc: Exception | None = None,
        stage: str | None = None,
    ) -> None:
        self.result = result
        self.exc = exc
        self.stage = stage
        self.progress = None
        self.calls: list[AnswerQuestionInput] = []

    async def answer(self, input_: AnswerQuestionInput) -> AnswerQuestionResult:
        self.calls.append(input_)
        if self.stage is not None:
            assert self.progress is not None
            await self.progress.stage_changed(self.stage)
        if self.exc is not None:
            raise self.exc
        assert self.result is not None
        return self.result


class FakeLiveEventPublisher:
    instances: list[FakeLiveEventPublisher] = []

    def __init__(self, redis: object, run_id: UUID) -> None:
        self.redis = redis
        self.run_id = run_id
        self.reset_calls = 0
        self.events: list[object] = []
        FakeLiveEventPublisher.instances.append(self)

    async def reset(self) -> None:
        self.reset_calls += 1

    async def event_occurred(self, event: object) -> None:
        self.events.append(event)


class FakeQuestionResolver:
    def __init__(self, outcome: ResolvedQuestionDraft | Exception) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, object]] = []

    async def resolve(self, **kwargs: object) -> ResolvedQuestionDraft:
        self.calls.append(kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _ctx(session_factory: async_sessionmaker[AsyncSession]) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(session_factory=session_factory))


@asynccontextmanager
async def _fake_http_client() -> object:
    yield object()


def _direct_result(answer: str = "worker answer") -> AnswerQuestionResult:
    return AnswerQuestionResult(
        status="answered",
        answer=answer,
        sources=[],
        missing_aspects=[],
        retrieval=AnswerRetrievalSummary(planned_mode="none"),
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
        retrieval=AnswerRetrievalSummary(planned_mode="external"),
    )


async def _create_thread_message_run(
    session: AsyncSession,
    *,
    status: str = "queued",
    question: str = "worker question",
    history: list[tuple[str, str]] | None = None,
    created_at: datetime | None = None,
    started_at: datetime | None = None,
    error_code: str | None = None,
) -> tuple[AgentThread, AgentMessage, AgentRun]:
    thread = AgentThread(
        user_id=UUID(TEST_USER_ID),
        title="thread",
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    session.add(thread)
    await session.flush()
    history = history or []
    for seq, (role, content) in enumerate(history, start=1):
        session.add(
            AgentMessage(
                thread_id=thread.id,
                seq=seq,
                role=role,
                content=content,
                missing_aspects=[],
            )
        )
    await session.flush()
    message = AgentMessage(
        thread_id=thread.id,
        seq=len(history) + 1,
        role="user",
        content=question,
        missing_aspects=[],
    )
    session.add(message)
    await session.flush()
    run = AgentRun(
        thread_id=thread.id,
        user_message_id=message.id,
        status=status,
        started_at=started_at,
        error_code=error_code,
    )
    if created_at is not None:
        run.created_at = created_at
    session.add(run)
    await session.commit()
    await session.refresh(thread)
    await session.refresh(message)
    await session.refresh(run)
    return thread, message, run


@pytest.mark.asyncio
async def test_run_agent_answer_completes_run_and_persists_assistant_message(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        thread, _message, run = await _create_thread_message_run(session)
    fake_agent = FakeAgent(_external_result())
    monkeypatch.setattr(agent_run_tasks, "make_safe_async_client", _fake_http_client)
    monkeypatch.setattr(
        agent_run_tasks,
        "build_question_answering_agent",
        lambda **_kwargs: fake_agent,
    )

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )

    async with session_factory() as session:
        completed = await session.get(AgentRun, run.id)
        assert completed is not None
        assert completed.status == "completed"
        assert completed.assistant_message_id is not None
        assistant = await session.get(AgentMessage, completed.assistant_message_id)
        assert assistant is not None
        assert assistant.seq == 2
        assert assistant.role == "assistant"
        assert assistant.content == "外部根拠つき回答。[[1]]"
        sources = (
            (
                await session.execute(
                    select(AgentMessageSource).where(
                        AgentMessageSource.message_id == assistant.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(sources) == 1
        assert sources[0].evidence_claim == "Agent claim."
        refreshed_thread = await session.get(AgentThread, thread.id)
        assert refreshed_thread is not None
        assert refreshed_thread.updated_at > datetime(2026, 1, 1, tzinfo=UTC)
    assert fake_agent.calls[0].question == "worker question"


@pytest.mark.asyncio
async def test_run_agent_answer_completion_preserves_last_progress_stage(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    fake_agent = FakeAgent(_direct_result(), stage="synthesizing")

    def build_agent(**kwargs: object) -> FakeAgent:
        fake_agent.progress = kwargs["progress"]
        return fake_agent

    monkeypatch.setattr(agent_run_tasks, "make_safe_async_client", _fake_http_client)
    monkeypatch.setattr(agent_run_tasks, "build_question_answering_agent", build_agent)

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )

    async with session_factory() as session:
        completed = await session.get(AgentRun, run.id)
        assert completed is not None
        assert completed.status == "completed"
        assert completed.progress_stage == "synthesizing"


@pytest.mark.asyncio
async def test_run_agent_answer_resets_live_events_and_injects_reporter(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    fake_agent = FakeAgent(_direct_result())
    captured_kwargs: dict[str, object] = {}
    FakeLiveEventPublisher.instances = []

    def build_agent(**kwargs: object) -> FakeAgent:
        captured_kwargs.update(kwargs)
        return fake_agent

    redis = object()
    monkeypatch.setattr(agent_run_tasks, "make_safe_async_client", _fake_http_client)
    monkeypatch.setattr(agent_run_tasks, "build_question_answering_agent", build_agent)
    monkeypatch.setattr(agent_run_tasks, "get_redis", lambda: redis)
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveEventPublisher",
        FakeLiveEventPublisher,
    )

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )

    assert len(FakeLiveEventPublisher.instances) == 1
    publisher = FakeLiveEventPublisher.instances[0]
    assert publisher.redis is redis
    assert publisher.run_id == run.id
    assert publisher.reset_calls == 1
    assert captured_kwargs["events"] is publisher


@pytest.mark.asyncio
async def test_run_agent_answer_resolves_history_and_publishes_non_echo_question(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(
            session,
            question="それの株価への影響は？",
            history=[
                ("user", "NVIDIA の発表を説明して"),
                ("assistant", "前回の回答 [[1]]"),
            ],
        )
    fake_agent = FakeAgent(_direct_result())
    resolver = FakeQuestionResolver(
        ResolvedQuestionDraft(
            standalone_question="NVIDIA の発表が株価へ与える影響は？",
            user_intent="投資判断向けに詳しく説明して",
            prior_coverage="発表内容は既に説明済み",
            user_activity_context="半導体投資を調査中",
        )
    )
    FakeLiveEventPublisher.instances = []
    monkeypatch.setattr(agent_run_tasks, "make_safe_async_client", _fake_http_client)
    monkeypatch.setattr(
        agent_run_tasks,
        "build_question_answering_agent",
        lambda **_kwargs: fake_agent,
    )
    monkeypatch.setattr(
        agent_run_tasks,
        "build_question_resolver",
        lambda: resolver,
    )
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveEventPublisher",
        FakeLiveEventPublisher,
    )

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )

    assert fake_agent.calls == [
        AnswerQuestionInput(
            question="NVIDIA の発表が株価へ与える影響は？",
            as_of=fake_agent.calls[0].as_of,
            user_intent="投資判断向けに詳しく説明して",
            prior_coverage="発表内容は既に説明済み",
            user_activity_context="半導体投資を調査中",
            previous_answer="前回の回答 [[1]]",
        )
    ]
    assert [message.content for message in resolver.calls[0]["history"]] == [
        "NVIDIA の発表を説明して",
        "前回の回答 [[1]]",
    ]
    publisher = FakeLiveEventPublisher.instances[0]
    assert len(publisher.events) == 1
    assert getattr(publisher.events[0], "type") == "question.resolved"
    assert getattr(publisher.events[0], "standalone_question") == (
        "NVIDIA の発表が株価へ与える影響は？"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        ResolvedQuestionDraft(standalone_question="それの株価への影響は？"),
        AIProviderError(),
    ],
)
async def test_run_agent_answer_does_not_publish_echo_or_fallback_resolution(
    outcome: ResolvedQuestionDraft | Exception,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    question = "それの株価への影響は？"
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(
            session,
            question=question,
            history=[("assistant", "前回の回答")],
        )
    fake_agent = FakeAgent(_direct_result())
    FakeLiveEventPublisher.instances = []
    monkeypatch.setattr(agent_run_tasks, "make_safe_async_client", _fake_http_client)
    monkeypatch.setattr(
        agent_run_tasks,
        "build_question_answering_agent",
        lambda **_kwargs: fake_agent,
    )
    monkeypatch.setattr(
        agent_run_tasks,
        "build_question_resolver",
        lambda: FakeQuestionResolver(outcome),
    )
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveEventPublisher",
        FakeLiveEventPublisher,
    )

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )

    assert fake_agent.calls[0].question == question
    assert fake_agent.calls[0].previous_answer == "前回の回答"
    assert FakeLiveEventPublisher.instances[0].events == []


@pytest.mark.asyncio
async def test_read_recent_messages_before_returns_bounded_oldest_first_thread_window(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        thread, message, _run = await _create_thread_message_run(
            session,
            history=[
                ("user", "old user"),
                ("assistant", "old assistant"),
                ("user", "latest prior user"),
            ],
        )

    async with session_factory() as session:
        messages = await AgentHistoryRepository(
            session
        ).read_recent_messages_before(
            thread_id=thread.id,
            before_seq=message.seq,
            limit=2,
        )

    assert [(item.role, item.content) for item in messages] == [
        ("assistant", "old assistant"),
        ("user", "latest prior user"),
    ]


@pytest.mark.asyncio
async def test_complete_run_warns_on_citation_source_mismatch_without_failing_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(
            session,
            status="running",
            started_at=datetime.now(UTC),
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
        retrieval=AnswerRetrievalSummary(planned_mode="external"),
    )

    with capture_logs() as logs:
        async with session_factory() as session:
            async with session.begin():
                completed = await AgentHistoryRepository(session).complete_run(
                    run_id=run.id,
                    result=result,
                )

    assert completed is True
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
async def test_run_agent_answer_generation_error_marks_failed_without_leaking_message(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    fake_agent = FakeAgent(exc=AIProviderError("SHOULD_NOT_LEAK"))
    monkeypatch.setattr(agent_run_tasks, "make_safe_async_client", _fake_http_client)
    monkeypatch.setattr(
        agent_run_tasks,
        "build_question_answering_agent",
        lambda **_kwargs: fake_agent,
    )

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )

    async with session_factory() as session:
        failed = await session.get(AgentRun, run.id)
        assert failed is not None
        assert failed.status == "failed"
        assert failed.error_code == "generation_unavailable"
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
        assert [m.role for m in messages] == ["user"]


@pytest.mark.asyncio
async def test_run_agent_answer_generation_error_preserves_death_progress_stage(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    fake_agent = FakeAgent(exc=AIProviderError("SHOULD_NOT_LEAK"), stage="retrieving")

    def build_agent(**kwargs: object) -> FakeAgent:
        fake_agent.progress = kwargs["progress"]
        return fake_agent

    monkeypatch.setattr(agent_run_tasks, "make_safe_async_client", _fake_http_client)
    monkeypatch.setattr(agent_run_tasks, "build_question_answering_agent", build_agent)

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )

    async with session_factory() as session:
        failed = await session.get(AgentRun, run.id)
        assert failed is not None
        assert failed.status == "failed"
        assert failed.error_code == "generation_unavailable"
        assert failed.progress_stage == "retrieving"


@pytest.mark.asyncio
async def test_run_agent_answer_pre_answer_build_failure_leaves_progress_stage_null(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)

    def build_agent(**_kwargs: object) -> None:
        raise AIProviderConfigurationError()

    monkeypatch.setattr(agent_run_tasks, "make_safe_async_client", _fake_http_client)
    monkeypatch.setattr(agent_run_tasks, "build_question_answering_agent", build_agent)

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )

    async with session_factory() as session:
        failed = await session.get(AgentRun, run.id)
        assert failed is not None
        assert failed.status == "failed"
        assert failed.error_code == "generation_unavailable"
        assert failed.progress_stage is None


@pytest.mark.asyncio
async def test_run_agent_answer_unexpected_error_marks_internal_error(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    fake_agent = FakeAgent(exc=RuntimeError("SHOULD_NOT_LEAK"))
    monkeypatch.setattr(agent_run_tasks, "make_safe_async_client", _fake_http_client)
    monkeypatch.setattr(
        agent_run_tasks,
        "build_question_answering_agent",
        lambda **_kwargs: fake_agent,
    )

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )

    async with session_factory() as session:
        failed = await session.get(AgentRun, run.id)
        assert failed is not None
        assert failed.status == "failed"
        assert failed.error_code == "internal_error"
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
        assert [m.role for m in messages] == ["user"]


@pytest.mark.asyncio
async def test_complete_run_lost_race_rolls_back_assistant_message(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as setup_session:
        _thread, _message, run = await _create_thread_message_run(
            setup_session, status="running"
        )
    stale_session = session_factory()
    try:
        async with stale_session.begin():
            stale_run = await stale_session.get(AgentRun, run.id)
            assert stale_run is not None and stale_run.status == "running"

        async with session_factory() as winner_session:
            async with winner_session.begin():
                await AgentHistoryRepository(winner_session).mark_failed(
                    run.id,
                    error_code=agent_run_tasks.AgentRunErrorCode.STALE,
                )

        with pytest.raises(RunTransitionLostError):
            async with stale_session.begin():
                await AgentHistoryRepository(stale_session).complete_run(
                    run_id=run.id,
                    result=_direct_result(),
                )
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
        assert [m.role for m in messages] == ["user"]


@pytest.mark.asyncio
async def test_acquire_for_execution_reexecutes_running_and_skips_terminal_runs(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)
    async with session_factory() as setup_session:
        _thread, _message, running = await _create_thread_message_run(
            setup_session,
            status="running",
            started_at=now - timedelta(minutes=11),
        )
        _terminal_thread, _terminal_message, failed = await _create_thread_message_run(
            setup_session,
            status="failed",
            error_code="internal_error",
        )

    async with session_factory() as session:
        async with session.begin():
            repo = AgentHistoryRepository(session)
            prepared = await repo.acquire_for_execution(running.id, now=now)
            skipped = await repo.acquire_for_execution(failed.id, now=now)

    assert prepared is not None
    assert prepared.run_id == running.id
    assert prepared.question == "worker question"
    assert prepared.user_message_seq == 1
    assert skipped is None
    async with session_factory() as session:
        reacquired = await session.get(AgentRun, running.id)
        terminal = await session.get(AgentRun, failed.id)
        assert reacquired is not None
        assert terminal is not None
        assert reacquired.status == "running"
        assert reacquired.started_at == now
        assert terminal.status == "failed"
        assert terminal.error_code == "internal_error"


@pytest.mark.asyncio
async def test_sweep_stale_agent_runs_marks_only_old_active_runs(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)
    async with session_factory() as session:
        _t1, _m1, old_queued = await _create_thread_message_run(
            session, created_at=now - timedelta(minutes=21)
        )
        _t2, _m2, old_running = await _create_thread_message_run(
            session,
            status="running",
            created_at=now - timedelta(minutes=30),
            started_at=now - timedelta(minutes=21),
        )
        _t3, _m3, fresh = await _create_thread_message_run(
            session, created_at=now - timedelta(minutes=19)
        )
        _t4, _m4, terminal = await _create_thread_message_run(
            session,
            status="failed",
            created_at=now - timedelta(minutes=30),
            error_code="internal_error",
        )
        count = await AgentHistoryRepository(session).sweep_stale_runs(now=now)
        await session.commit()
        assert count == 2

    async with session_factory() as session:
        swept_queued = await session.get(AgentRun, old_queued.id)
        swept_running = await session.get(AgentRun, old_running.id)
        untouched_fresh = await session.get(AgentRun, fresh.id)
        untouched_terminal = await session.get(AgentRun, terminal.id)
        assert swept_queued is not None
        assert swept_running is not None
        assert untouched_fresh is not None
        assert untouched_terminal is not None
        assert swept_queued.status == "failed"
        assert swept_running.error_code == "stale"
        assert untouched_fresh.status == "queued"
        assert untouched_terminal.error_code == "internal_error"


def test_source_mapper_rejects_user_message() -> None:
    message = AgentMessage(
        thread_id=UUID("00000000-0000-4000-a000-000000000001"),
        seq=1,
        role="user",
        content="question",
        missing_aspects=[],
    )

    with pytest.raises(ValueError, match="assistant messages"):
        build_source_rows_for_message(message, _direct_result())


def test_source_mapper_structures_internal_and_external_rows() -> None:
    result = AnswerQuestionResult(
        status="answered",
        answer="answer [[1]][[2]]",
        sources=[
            InternalArticleSource(source_ref="1", article_id=123, title="Internal"),
            ExternalUrlSource(
                source_ref="2",
                url=SafeUrl("https://example.com/e"),
                title="External",
                evidence_claim="Claim",
            ),
        ],
        missing_aspects=[],
        retrieval=AnswerRetrievalSummary(planned_mode="internal_and_external"),
    )
    message = build_assistant_message_for_result(
        thread_id=UUID("00000000-0000-4000-a000-000000000001"),
        seq=2,
        result=result,
    )
    message.id = UUID("00000000-0000-4000-a000-000000000010")
    message.created_at = datetime(2026, 7, 9, tzinfo=UTC)

    rows = build_source_rows_for_message(message, result)

    assert message.role == "assistant"
    assert message.content == "answer [[1]][[2]]"
    assert rows[0].analyzed_article_id == 123
    assert rows[0].url is None
    assert rows[0].evidence_claim is None
    assert rows[1].url == "https://example.com/e"
    assert rows[1].analyzed_article_id is None
    assert rows[1].evidence_claim == "Claim"

    response = build_research_assistant_message(message=message, sources=rows)
    assert response.content == "answer [[1]][[2]]"
    assert response.sources[0].kind == "internal_article"
    assert response.sources[1].kind == "external_url"
