"""Agent run worker and conversation/run repository tests."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

import app.queue.tasks.agent_run as agent_run_tasks
from app.agent.answering.direct_answer.contract import DirectAnswerInvalidError
from app.agent.contract import (
    AnswerQuestionInput,
    AnswerQuestionResult,
    AnswerRetrievalSummary,
    ExternalUrlSource,
    InternalArticleSource,
)
from app.agent.live_updates.reporters import AgentRunLiveActivityReporter
from app.agent.live_updates.stream import (
    AgentRunLiveStreamActivityEvent,
    AgentRunLiveStreamStageEvent,
    AgentRunLiveStreamTerminalEvent,
)
from app.agent.question_resolution.contract import ResolvedQuestionDraft
from app.agent.runs.contracts import (
    CancelRunOutcome,
    CancelRunResult,
    RunTransitionLostError,
)
from app.agent.runs.repository import AgentRunRepository
from app.agent.runs.result_mapper import (
    build_assistant_message_for_result,
    build_source_rows_for_message,
)
from app.agent.runs.types import AgentRunStatus
from app.agent.threads.projection import build_research_assistant_message
from app.agent.threads.repository import AgentThreadRepository
from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderError,
)
from app.models.agent_message import AgentMessage, AgentMessageSource
from app.models.agent_run import AgentRun
from app.models.agent_thread import AgentThread
from app.queue.messages.agent_run import AgentRunTrigger
from app.shared.security.safe_url import SafeUrl
from tests.conftest import TEST_ADMIN_ID, TEST_USER_ID


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


class FakeLiveStreamPublisher:
    instances: list[FakeLiveStreamPublisher] = []
    raise_on_begin = False
    raise_on_publish = False

    def __init__(self, redis: object, run_id: UUID, attempt_epoch: int) -> None:
        self.redis = redis
        self.run_id = run_id
        self.attempt_epoch = attempt_epoch
        self.begin_attempt_calls = 0
        self.published: list[object] = []
        FakeLiveStreamPublisher.instances.append(self)

    async def begin_attempt(self) -> None:
        self.begin_attempt_calls += 1
        if self.raise_on_begin:
            raise RuntimeError("Redis unavailable")

    async def publish(self, event: object) -> None:
        self.published.append(event)
        if self.raise_on_publish:
            raise RuntimeError("Redis unavailable")


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
    attempt_epoch: int | None = None,
    error_code: str | None = None,
    user_id: str = TEST_USER_ID,
) -> tuple[AgentThread, AgentMessage, AgentRun]:
    thread = AgentThread(
        user_id=UUID(user_id),
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
    if attempt_epoch is not None:
        run.attempt_epoch = attempt_epoch
    if created_at is not None:
        run.created_at = created_at
    session.add(run)
    await session.commit()
    await session.refresh(thread)
    await session.refresh(message)
    await session.refresh(run)
    return thread, message, run


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
async def test_cancel_returns_epoch_from_winning_update_during_acquire_race(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as setup_session:
        _thread, _message, run = await _create_thread_message_run(setup_session)

    initial_select_finished = asyncio.Event()
    allow_cancel_update = asyncio.Event()

    class PausingSession:
        def __init__(self, session: AsyncSession) -> None:
            self._session = session
            self.execute_calls = 0

        async def execute(self, *args: object, **kwargs: object) -> object:
            result = await self._session.execute(*args, **kwargs)
            self.execute_calls += 1
            if self.execute_calls == 1:
                initial_select_finished.set()
                await allow_cancel_update.wait()
            return result

    async def cancel() -> CancelRunResult | None:
        async with session_factory() as raw_session:
            paused = cast(AsyncSession, PausingSession(raw_session))
            async with raw_session.begin():
                return await AgentRunRepository(paused).cancel_run_for_user(
                    run_id=run.id,
                    user_id=UUID(TEST_USER_ID),
                )

    cancel_task = asyncio.create_task(cancel())
    await asyncio.wait_for(initial_select_finished.wait(), timeout=1)
    try:
        async with session_factory() as acquire_session:
            async with acquire_session.begin():
                prepared = await AgentRunRepository(
                    acquire_session
                ).acquire_for_execution(run.id)
        assert prepared is not None
        assert prepared.attempt_epoch == 1
    finally:
        allow_cancel_update.set()

    result = await asyncio.wait_for(cancel_task, timeout=2)

    assert result == CancelRunResult(
        outcome=CancelRunOutcome.CANCELLED,
        attempt_epoch=1,
    )
    async with session_factory() as session:
        cancelled = await session.get(AgentRun, run.id)
        assert cancelled is not None
        assert cancelled.status == "failed"
        assert cancelled.error_code == "cancelled"
        assert cancelled.attempt_epoch == 1


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
    FakeLiveStreamPublisher.instances = []

    def build_agent(**kwargs: object) -> FakeAgent:
        fake_agent.progress = kwargs["progress"]
        return fake_agent

    monkeypatch.setattr(agent_run_tasks, "make_safe_async_client", _fake_http_client)
    monkeypatch.setattr(agent_run_tasks, "build_question_answering_agent", build_agent)
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveEventPublisher",
        FakeLiveEventPublisher,
    )
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveStreamPublisher",
        FakeLiveStreamPublisher,
    )

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )

    async with session_factory() as session:
        completed = await session.get(AgentRun, run.id)
        assert completed is not None
        assert completed.status == "completed"
        assert completed.progress_stage == "synthesizing"
    stream = FakeLiveStreamPublisher.instances[0]
    stages = [
        event
        for event in stream.published
        if isinstance(event, AgentRunLiveStreamStageEvent)
    ]
    assert [event.stage for event in stages] == ["synthesizing"]


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
    assert isinstance(captured_kwargs["events"], AgentRunLiveActivityReporter)


@pytest.mark.asyncio
async def test_run_agent_answer_starts_stream_attempt_only_after_acquire(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    fake_agent = FakeAgent(_direct_result())
    redis = object()
    FakeLiveStreamPublisher.instances = []
    FakeLiveStreamPublisher.raise_on_begin = False

    def build_agent(**_kwargs: object) -> FakeAgent:
        assert len(FakeLiveStreamPublisher.instances) == 1
        assert FakeLiveStreamPublisher.instances[0].begin_attempt_calls == 1
        return fake_agent

    monkeypatch.setattr(agent_run_tasks, "make_safe_async_client", _fake_http_client)
    monkeypatch.setattr(agent_run_tasks, "build_question_answering_agent", build_agent)
    monkeypatch.setattr(agent_run_tasks, "get_redis", lambda: redis)
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveEventPublisher",
        FakeLiveEventPublisher,
    )
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveStreamPublisher",
        FakeLiveStreamPublisher,
        raising=False,
    )

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )

    assert len(FakeLiveStreamPublisher.instances) == 1
    publisher = FakeLiveStreamPublisher.instances[0]
    assert publisher.redis is redis
    assert publisher.run_id == run.id
    assert publisher.attempt_epoch == 1
    assert publisher.begin_attempt_calls == 1


@pytest.mark.asyncio
async def test_run_agent_answer_continues_when_stream_begin_attempt_raises(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    fake_agent = FakeAgent(_direct_result())
    FakeLiveStreamPublisher.instances = []
    monkeypatch.setattr(FakeLiveStreamPublisher, "raise_on_begin", True)
    monkeypatch.setattr(agent_run_tasks, "make_safe_async_client", _fake_http_client)
    monkeypatch.setattr(
        agent_run_tasks,
        "build_question_answering_agent",
        lambda **_kwargs: fake_agent,
    )
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveEventPublisher",
        FakeLiveEventPublisher,
    )
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveStreamPublisher",
        FakeLiveStreamPublisher,
        raising=False,
    )

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )

    assert FakeLiveStreamPublisher.instances[0].begin_attempt_calls == 1
    async with session_factory() as session:
        completed = await session.get(AgentRun, run.id)
        assert completed is not None
        assert completed.status == "completed"


@pytest.mark.asyncio
async def test_idempotent_skip_does_not_create_or_start_stream_publisher(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(
            session,
            status="failed",
            error_code="internal_error",
        )
    FakeLiveStreamPublisher.instances = []
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveStreamPublisher",
        FakeLiveStreamPublisher,
        raising=False,
    )

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )

    assert FakeLiveStreamPublisher.instances == []


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
    FakeLiveStreamPublisher.instances = []
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
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveStreamPublisher",
        FakeLiveStreamPublisher,
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
    stream_activities = [
        event
        for event in FakeLiveStreamPublisher.instances[0].published
        if isinstance(event, AgentRunLiveStreamActivityEvent)
    ]
    assert len(stream_activities) == 1
    assert stream_activities[0].activity == publisher.events[0]


@pytest.mark.asyncio
async def test_run_agent_answer_publishes_completed_terminal_after_commit(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    fake_agent = FakeAgent(_direct_result())
    FakeLiveStreamPublisher.instances = []

    class CommitCheckingPublisher(FakeLiveStreamPublisher):
        async def publish(self, event: object) -> None:
            if isinstance(event, AgentRunLiveStreamTerminalEvent):
                async with session_factory() as session:
                    persisted = await session.get(AgentRun, run.id)
                    assert persisted is not None
                    assert persisted.status == "completed"
            await super().publish(event)

    monkeypatch.setattr(agent_run_tasks, "make_safe_async_client", _fake_http_client)
    monkeypatch.setattr(
        agent_run_tasks,
        "build_question_answering_agent",
        lambda **_kwargs: fake_agent,
    )
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveEventPublisher",
        FakeLiveEventPublisher,
    )
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveStreamPublisher",
        CommitCheckingPublisher,
    )

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )

    terminal = [
        event
        for event in FakeLiveStreamPublisher.instances[0].published
        if isinstance(event, AgentRunLiveStreamTerminalEvent)
    ]
    assert terminal == [AgentRunLiveStreamTerminalEvent(status="completed")]


@pytest.mark.asyncio
async def test_run_agent_answer_publishes_failed_terminal_after_commit(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    fake_agent = FakeAgent(exc=AIProviderError("provider unavailable"))
    FakeLiveStreamPublisher.instances = []

    class CommitCheckingPublisher(FakeLiveStreamPublisher):
        async def publish(self, event: object) -> None:
            if isinstance(event, AgentRunLiveStreamTerminalEvent):
                async with session_factory() as session:
                    persisted = await session.get(AgentRun, run.id)
                    assert persisted is not None
                    assert persisted.status == "failed"
                    assert persisted.error_code == "generation_unavailable"
            await super().publish(event)

    monkeypatch.setattr(agent_run_tasks, "make_safe_async_client", _fake_http_client)
    monkeypatch.setattr(
        agent_run_tasks,
        "build_question_answering_agent",
        lambda **_kwargs: fake_agent,
    )
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveEventPublisher",
        FakeLiveEventPublisher,
    )
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveStreamPublisher",
        CommitCheckingPublisher,
    )

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )

    terminal = [
        event
        for event in FakeLiveStreamPublisher.instances[0].published
        if isinstance(event, AgentRunLiveStreamTerminalEvent)
    ]
    assert terminal == [
        AgentRunLiveStreamTerminalEvent(
            status="failed",
            errorCode="generation_unavailable",
        )
    ]


@pytest.mark.asyncio
async def test_completion_failure_uses_failed_terminal_choke_point(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    fake_agent = FakeAgent(_direct_result())
    FakeLiveStreamPublisher.instances = []

    async def fail_completion(
        _repo: AgentRunRepository,
        **_kwargs: object,
    ) -> bool:
        raise RuntimeError("completion failed")

    monkeypatch.setattr(agent_run_tasks, "make_safe_async_client", _fake_http_client)
    monkeypatch.setattr(
        agent_run_tasks,
        "build_question_answering_agent",
        lambda **_kwargs: fake_agent,
    )
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveEventPublisher",
        FakeLiveEventPublisher,
    )
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveStreamPublisher",
        FakeLiveStreamPublisher,
    )
    monkeypatch.setattr(AgentRunRepository, "complete_run", fail_completion)

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )

    async with session_factory() as session:
        persisted = await session.get(AgentRun, run.id)
        assert persisted is not None
        assert persisted.status == "failed"
        assert persisted.error_code == "internal_error"
    terminal = [
        event
        for event in FakeLiveStreamPublisher.instances[0].published
        if isinstance(event, AgentRunLiveStreamTerminalEvent)
    ]
    assert terminal == [
        AgentRunLiveStreamTerminalEvent(
            status="failed",
            errorCode="internal_error",
        )
    ]


@pytest.mark.asyncio
async def test_completion_transition_loser_does_not_publish_terminal(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    fake_agent = FakeAgent(_direct_result())
    FakeLiveStreamPublisher.instances = []

    async def lose_completion(
        _repo: AgentRunRepository,
        **_kwargs: object,
    ) -> bool:
        raise RunTransitionLostError

    monkeypatch.setattr(agent_run_tasks, "make_safe_async_client", _fake_http_client)
    monkeypatch.setattr(
        agent_run_tasks,
        "build_question_answering_agent",
        lambda **_kwargs: fake_agent,
    )
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveEventPublisher",
        FakeLiveEventPublisher,
    )
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveStreamPublisher",
        FakeLiveStreamPublisher,
    )
    monkeypatch.setattr(AgentRunRepository, "complete_run", lose_completion)

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )

    terminal = [
        event
        for event in FakeLiveStreamPublisher.instances[0].published
        if isinstance(event, AgentRunLiveStreamTerminalEvent)
    ]
    assert terminal == []


@pytest.mark.asyncio
async def test_completion_skip_does_not_publish_terminal(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    fake_agent = FakeAgent(_direct_result())
    FakeLiveStreamPublisher.instances = []

    async def skip_completion(
        _repo: AgentRunRepository,
        **_kwargs: object,
    ) -> bool:
        return False

    monkeypatch.setattr(agent_run_tasks, "make_safe_async_client", _fake_http_client)
    monkeypatch.setattr(
        agent_run_tasks,
        "build_question_answering_agent",
        lambda **_kwargs: fake_agent,
    )
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveEventPublisher",
        FakeLiveEventPublisher,
    )
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveStreamPublisher",
        FakeLiveStreamPublisher,
    )
    monkeypatch.setattr(AgentRunRepository, "complete_run", skip_completion)

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )

    terminal = [
        event
        for event in FakeLiveStreamPublisher.instances[0].published
        if isinstance(event, AgentRunLiveStreamTerminalEvent)
    ]
    assert terminal == []


@pytest.mark.asyncio
async def test_failed_transition_loser_does_not_publish_terminal(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    fake_agent = FakeAgent(exc=AIProviderError("provider unavailable"))
    FakeLiveStreamPublisher.instances = []

    async def lose_transition(
        _repo: AgentRunRepository,
        _run_id: UUID,
        **_kwargs: object,
    ) -> bool:
        return False

    monkeypatch.setattr(agent_run_tasks, "make_safe_async_client", _fake_http_client)
    monkeypatch.setattr(
        agent_run_tasks,
        "build_question_answering_agent",
        lambda **_kwargs: fake_agent,
    )
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveEventPublisher",
        FakeLiveEventPublisher,
    )
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveStreamPublisher",
        FakeLiveStreamPublisher,
    )
    monkeypatch.setattr(AgentRunRepository, "mark_failed", lose_transition)

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )

    terminal = [
        event
        for event in FakeLiveStreamPublisher.instances[0].published
        if isinstance(event, AgentRunLiveStreamTerminalEvent)
    ]
    assert terminal == []


@pytest.mark.asyncio
async def test_terminal_publish_failure_does_not_revert_completed_run(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    fake_agent = FakeAgent(_direct_result(), stage="synthesizing")
    FakeLiveStreamPublisher.instances = []
    monkeypatch.setattr(FakeLiveStreamPublisher, "raise_on_publish", True)

    def build_agent(**kwargs: object) -> FakeAgent:
        fake_agent.progress = kwargs["progress"]
        return fake_agent

    monkeypatch.setattr(agent_run_tasks, "make_safe_async_client", _fake_http_client)
    monkeypatch.setattr(
        agent_run_tasks,
        "build_question_answering_agent",
        build_agent,
    )
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveEventPublisher",
        FakeLiveEventPublisher,
    )
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveStreamPublisher",
        FakeLiveStreamPublisher,
    )

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )

    async with session_factory() as session:
        persisted = await session.get(AgentRun, run.id)
        assert persisted is not None
        assert persisted.status == "completed"
    assert any(
        isinstance(event, AgentRunLiveStreamTerminalEvent)
        for event in FakeLiveStreamPublisher.instances[0].published
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
        messages = await AgentThreadRepository(session).read_recent_messages_before(
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
                completed = await AgentRunRepository(session).complete_run(
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
@pytest.mark.parametrize(
    "generation_error",
    [AIProviderError("SHOULD_NOT_LEAK"), DirectAnswerInvalidError()],
    ids=("provider", "direct-draft"),
)
async def test_run_agent_answer_generation_error_marks_failed_without_leaking_message(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    generation_error: Exception,
) -> None:
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    fake_agent = FakeAgent(exc=generation_error)
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
    FakeLiveStreamPublisher.instances = []
    monkeypatch.setattr(agent_run_tasks, "make_safe_async_client", _fake_http_client)
    monkeypatch.setattr(
        agent_run_tasks,
        "build_question_answering_agent",
        lambda **_kwargs: fake_agent,
    )
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveEventPublisher",
        FakeLiveEventPublisher,
    )
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveStreamPublisher",
        FakeLiveStreamPublisher,
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
    terminal = [
        event
        for event in FakeLiveStreamPublisher.instances[0].published
        if isinstance(event, AgentRunLiveStreamTerminalEvent)
    ]
    assert terminal == [
        AgentRunLiveStreamTerminalEvent(
            status="failed",
            errorCode="internal_error",
        )
    ]


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
                await AgentRunRepository(winner_session).mark_failed(
                    run.id,
                    error_code=agent_run_tasks.AgentRunErrorCode.STALE,
                )

        with pytest.raises(RunTransitionLostError):
            async with stale_session.begin():
                await AgentRunRepository(stale_session).complete_run(
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
            prepared = await repo.acquire_for_execution(running.id, now=now)
            skipped = await repo.acquire_for_execution(failed.id, now=now)

    assert prepared is not None
    assert prepared.run_id == running.id
    assert prepared.question == "worker question"
    assert prepared.user_message_seq == 1
    assert prepared.attempt_epoch == 2
    assert skipped is None
    async with session_factory() as session:
        reacquired = await session.get(AgentRun, running.id)
        terminal = await session.get(AgentRun, failed.id)
        assert reacquired is not None
        assert terminal is not None
        assert reacquired.status == "running"
        assert reacquired.started_at == now
        assert reacquired.attempt_epoch == 2
        assert terminal.status == "failed"
        assert terminal.error_code == "internal_error"
        assert terminal.attempt_epoch == 0


@pytest.mark.asyncio
async def test_acquire_for_execution_allocates_first_attempt_epoch(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as setup_session:
        _thread, _message, run = await _create_thread_message_run(setup_session)

    async with session_factory() as session:
        async with session.begin():
            prepared = await AgentRunRepository(session).acquire_for_execution(run.id)

    async with session_factory() as session:
        acquired = await session.get(AgentRun, run.id)
        assert prepared is not None
        assert acquired is not None
        assert prepared.attempt_epoch == 1
        assert acquired.attempt_epoch == 1


@pytest.mark.asyncio
async def test_acquire_for_execution_increment_rolls_back_with_transaction(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as setup_session:
        _thread, _message, run = await _create_thread_message_run(setup_session)

    async with session_factory() as session:
        prepared = await AgentRunRepository(session).acquire_for_execution(run.id)
        assert prepared is not None
        assert prepared.attempt_epoch == 1
        await session.rollback()

    async with session_factory() as session:
        unchanged = await session.get(AgentRun, run.id)
        assert unchanged is not None
        assert unchanged.status == "queued"
        assert unchanged.attempt_epoch == 0


@pytest.mark.asyncio
async def test_concurrent_acquisitions_receive_distinct_sequence_values(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as setup_session:
        _thread, _message, run = await _create_thread_message_run(setup_session)

    selected_count = 0
    selected_lock = asyncio.Lock()
    both_selected = asyncio.Event()
    release_updates = asyncio.Event()

    async def acquire() -> int:
        nonlocal selected_count
        async with session_factory() as session:
            original_execute = session.execute
            execute_count = 0

            async def execute_with_barrier(*args: object, **kwargs: object) -> object:
                nonlocal execute_count, selected_count
                result = await original_execute(*args, **kwargs)  # type: ignore[arg-type]
                execute_count += 1
                if execute_count == 1:
                    async with selected_lock:
                        selected_count += 1
                        if selected_count == 2:
                            both_selected.set()
                    await release_updates.wait()
                return result

            monkeypatch.setattr(session, "execute", execute_with_barrier)
            async with session.begin():
                prepared = await AgentRunRepository(session).acquire_for_execution(
                    run.id
                )
                assert prepared is not None
                return prepared.attempt_epoch

    acquire_tasks = [asyncio.create_task(acquire()), asyncio.create_task(acquire())]
    try:
        await asyncio.wait_for(both_selected.wait(), timeout=1)
    finally:
        release_updates.set()
    epochs = await asyncio.gather(*acquire_tasks)

    assert sorted(epochs) == [1, 2]
    async with session_factory() as session:
        acquired = await session.get(AgentRun, run.id)
        assert acquired is not None
        assert acquired.attempt_epoch == 2


@pytest.mark.asyncio
async def test_acquire_for_execution_returns_none_for_missing_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    missing_run_id = UUID("00000000-0000-4000-a000-000000000099")

    async with session_factory() as session:
        async with session.begin():
            prepared = await AgentRunRepository(session).acquire_for_execution(
                missing_run_id
            )

    assert prepared is None


@pytest.mark.asyncio
async def test_acquire_for_execution_returns_none_when_transition_loses_race(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as setup_session:
        _thread, _message, run = await _create_thread_message_run(setup_session)

    selected = asyncio.Event()
    resume = asyncio.Event()
    contender = session_factory()
    original_execute = contender.execute
    execute_count = 0

    async def execute_with_pause(*args: object, **kwargs: object) -> object:
        nonlocal execute_count
        result = await original_execute(*args, **kwargs)  # type: ignore[arg-type]
        execute_count += 1
        if execute_count == 1:
            selected.set()
            await resume.wait()
        return result

    monkeypatch.setattr(contender, "execute", execute_with_pause)
    try:

        async def acquire() -> object:
            async with contender.begin():
                return await AgentRunRepository(contender).acquire_for_execution(run.id)

        acquire_task = asyncio.create_task(acquire())
        await selected.wait()
        async with session_factory() as winner:
            async with winner.begin():
                changed = await AgentRunRepository(winner).mark_failed(
                    run.id,
                    error_code=agent_run_tasks.AgentRunErrorCode.STALE,
                )
                assert changed is True
        resume.set()
        prepared = await acquire_task
    finally:
        resume.set()
        await contender.close()

    assert prepared is None
    async with session_factory() as session:
        failed = await session.get(AgentRun, run.id)
        assert failed is not None
        assert failed.status == "failed"
        assert failed.attempt_epoch == 0


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
        count = await AgentRunRepository(session).sweep_stale_runs(now=now)
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
