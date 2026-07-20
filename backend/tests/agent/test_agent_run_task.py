"""Agent run worker and conversation/run repository tests."""

from __future__ import annotations

import ast
import asyncio
import inspect
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from logfire.testing import CaptureLogfire
from sqlalchemy import event as sa_event
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

import app.agent.composition as composition
import app.queue.tasks.agent_run as agent_run_tasks
from app.agent.answering.direct_answer.contract import (
    DirectAnswerInvalidError,
)
from app.agent.contract import (
    AnswerGenerationStopped,
    AnswerQuestionResult,
    AnswerRetrievalSummary,
    ExternalUrlSource,
    InternalArticleSource,
)
from app.agent.live_updates.reporters import AgentRunLiveActivityReporter
from app.agent.live_updates.stream import (
    AgentRunLiveStreamActivityEvent,
    AgentRunLiveStreamAnswerDeltaEvent,
    AgentRunLiveStreamAnswerResetEvent,
    AgentRunLiveStreamStageEvent,
    AgentRunLiveStreamTerminalEvent,
)
from app.agent.question_context.contract import AnswerRequirement, QuestionContext
from app.agent.running import (
    AnsweringPhases,
    AnsweringRunContext,
    QuestionResolvedRunHooks,
    RunContext,
    RunInput,
    RunResult,
)
from app.agent.runs.contracts import (
    RunTransitionLostError,
)
from app.agent.runs.daily_quota import observability as daily_quota_observability
from app.agent.runs.repository import AgentRunRepository
from app.agent.runs.result_mapper import (
    build_assistant_message_for_result,
    build_source_rows_for_message,
)
from app.agent.runs.types import AgentRunStatus
from app.agent.threads.contracts import ThreadMessageSnapshot
from app.agent.threads.projection import build_research_assistant_message
from app.agent.threads.repository import AgentThreadRepository
from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderError,
)
from app.models.agent_message import AgentMessage, AgentMessageSource
from app.models.agent_run import AgentRun
from app.models.agent_thread import AgentThread
from app.models.agent_user_daily_quota import AgentUserDailyQuota
from app.queue.messages.agent_run import AgentRunTrigger
from app.shared.security.safe_url import SafeUrl
from tests.conftest import TEST_ADMIN_ID, TEST_USER_ID
from tests.logfire._metric_helpers import collected_metrics


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
        self.calls: list[AnsweringRunContext] = []

    async def answer(self, input_: AnsweringRunContext) -> AnswerQuestionResult:
        self.calls.append(input_)
        if self.stage is not None:
            assert self.progress is not None
            await self.progress.stage_changed(self.stage)
        if self.exc is not None:
            raise self.exc
        assert self.result is not None
        return self.result


@dataclass(frozen=True, slots=True)
class FakeAnsweringRunnerCall:
    input: RunInput
    run_context: RunContext
    hooks: object | None


class FakeAnsweringRunner:
    def __init__(
        self,
        *,
        exc: BaseException | None = None,
        question_context: QuestionContext | None = None,
        previous_answer: str = "",
    ) -> None:
        self.exc = exc
        self.question_context = question_context
        self.previous_answer = previous_answer
        self.execution: object | None = None
        self.calls: list[FakeAnsweringRunnerCall] = []

    async def run(
        self,
        input: RunInput,
        *,
        run_context: RunContext,
        hooks: object | None = None,
    ) -> RunResult:
        self.calls.append(
            FakeAnsweringRunnerCall(
                input=input,
                run_context=run_context,
                hooks=hooks,
            )
        )
        if self.exc is not None:
            raise self.exc
        question_context = self.question_context or QuestionContext(
            standalone_question=input.question
        )
        answering_context = AnsweringRunContext(
            run_context=run_context,
            question_context=question_context,
            previous_answer=self.previous_answer,
        )
        if hooks is not None:
            await cast(Any, hooks).on_answering_context_prepared(
                original_question=input.question,
                has_history=bool(input.history),
                question_context=question_context,
            )
        assert self.execution is not None
        final_output = await cast(Any, self.execution).answer(answering_context)
        return RunResult(final_output=final_output, context=answering_context)


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
    publish_outcomes: list[str | None | BaseException] = []

    def __init__(self, redis: object, run_id: UUID, attempt_epoch: int) -> None:
        self.redis = redis
        self.run_id = run_id
        self.attempt_epoch = attempt_epoch
        self.begin_attempt_calls = 0
        self.published: list[object] = []
        FakeLiveStreamPublisher.instances.append(self)

    async def begin_attempt(self) -> str | None:
        self.begin_attempt_calls += 1
        if self.raise_on_begin:
            raise RuntimeError("Redis unavailable")
        return "attempt-0"

    async def publish(self, event: object) -> str | None:
        self.published.append(event)
        if self.raise_on_publish:
            raise RuntimeError("Redis unavailable")
        if self.publish_outcomes:
            outcome = self.publish_outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return f"{len(self.published)}-0"


class DeltaReportingAgent:
    def __init__(
        self,
        *,
        result: AnswerQuestionResult | None = None,
        exc: Exception | None = None,
        fragments: list[str] | None = None,
        finish: bool = True,
        order: list[str] | None = None,
    ) -> None:
        self.result = result
        self.exc = exc
        self.fragments = fragments or []
        self.finish = finish
        self.order = order
        self.delta_reporter: object | None = None

    async def answer(self, _input: AnsweringRunContext) -> AnswerQuestionResult:
        assert self.delta_reporter is not None
        for fragment in self.fragments:
            await self.delta_reporter.append(generation=1, text=fragment)  # type: ignore[attr-defined]
        if self.finish:
            await self.delta_reporter.finish(generation=1)  # type: ignore[attr-defined]
            if self.order is not None:
                self.order.append("delta_finish")
        if self.exc is not None:
            raise self.exc
        assert self.result is not None
        return self.result


class RevisionReportingAgent:
    def __init__(self, result: AnswerQuestionResult, text: str) -> None:
        self.result = result
        self.text = text
        self.delta_reporter: object | None = None
        self.continuation: object | None = None

    async def answer(self, _input: AnsweringRunContext) -> AnswerQuestionResult:
        assert self.delta_reporter is not None
        assert self.continuation is not None
        await self.delta_reporter.reset(generation=2)  # type: ignore[attr-defined]
        await self.delta_reporter.append(  # type: ignore[attr-defined]
            generation=2,
            text=self.text,
        )
        await self.delta_reporter.finish(generation=2)  # type: ignore[attr-defined]
        return self.result


class CapturingDeltaReporter:
    instances: list[CapturingDeltaReporter] = []

    def __init__(
        self,
        publisher: object,
        *,
        run_id: UUID,
        attempt_epoch: int,
    ) -> None:
        self.publisher = publisher
        self.run_id = run_id
        self.attempt_epoch = attempt_epoch
        CapturingDeltaReporter.instances.append(self)


class CapturingExecutionProbe:
    instances: list[CapturingExecutionProbe] = []

    def __init__(
        self,
        session_factory: object,
        run_id: UUID,
        attempt_epoch: int,
    ) -> None:
        self.session_factory = session_factory
        self.run_id = run_id
        self.attempt_epoch = attempt_epoch
        CapturingExecutionProbe.instances.append(self)


class CapturingProgressWriter:
    instances: list[CapturingProgressWriter] = []

    def __init__(
        self,
        session_factory: object,
        run_id: UUID,
        attempt_epoch: int,
    ) -> None:
        self.session_factory = session_factory
        self.run_id = run_id
        self.attempt_epoch = attempt_epoch
        CapturingProgressWriter.instances.append(self)

    async def stage_changed(self, _stage: object) -> None:
        return None


class ForbiddenConstruction:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("acquire skip後にlive dependencyを生成してはいけません")


def _ctx(session_factory: async_sessionmaker[AsyncSession]) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(session_factory=session_factory))


def _quota_stale_metric_points(capfire: CaptureLogfire) -> list[dict[str, Any]]:
    metric = next(
        (
            item
            for item in collected_metrics(capfire)
            if item["name"] == "agent_user_daily_quota_stale_reservations_total"
        ),
        None,
    )
    if metric is None:
        return []
    return list(metric["data"]["data_points"])


def _patch_worker_execution(
    monkeypatch: pytest.MonkeyPatch,
    execution_builder: object,
    *,
    answering_runner: FakeAnsweringRunner | None = None,
) -> FakeAnsweringRunner:
    answering_runner = answering_runner or FakeAnsweringRunner()

    def build_runner(**kwargs: object) -> FakeAnsweringRunner:
        answering_runner.execution = cast(Any, execution_builder)(**kwargs)
        return answering_runner

    monkeypatch.setattr(
        agent_run_tasks,
        "build_answering_runner",
        build_runner,
        raising=False,
    )
    return answering_runner


def _patch_delta_worker(
    monkeypatch: pytest.MonkeyPatch,
    builder: object,
    *,
    stream_publisher: type[FakeLiveStreamPublisher] = FakeLiveStreamPublisher,
) -> None:
    FakeLiveEventPublisher.instances = []
    FakeLiveStreamPublisher.instances = []
    monkeypatch.setattr(FakeLiveStreamPublisher, "publish_outcomes", [])
    _patch_worker_execution(monkeypatch, builder)
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveEventPublisher",
        FakeLiveEventPublisher,
    )
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveStreamPublisher",
        stream_publisher,
    )


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


def test_composition_injects_same_live_controls_into_both_answer_flows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.agent.answering.direct_answer.flow as direct_flow_module
    import app.agent.answering.evidence_answer.flow as evidence_flow_module
    import app.agent.evidence_collection.internal_search.ai.gemini as embedder_module
    import app.agent.planning.service as planning_service_module
    from app.agent.answering.direct_answer.agent import DIRECT_ANSWER_AGENT
    from app.agent.answering.evidence_answer.agent import EVIDENCE_ANSWER_AGENT
    from app.agent.evidence_collection.internal_search import (
        article_search as article_search_module,
    )
    from app.agent.evidence_collection.internal_search import (
        service as internal_search_module,
    )

    captured: dict[str, dict[str, object]] = {}
    external_runtime_factory = object()
    internal_search = object()

    def capture_direct(**kwargs: object) -> object:
        captured["direct"] = kwargs
        return object()

    def capture_evidence(**kwargs: object) -> object:
        captured["evidence"] = kwargs
        return object()

    monkeypatch.setattr(
        composition,
        "ensure_external_search_configured",
        lambda: None,
    )
    monkeypatch.setattr(
        composition,
        "build_external_research_runtime_factory",
        lambda: external_runtime_factory,
    )
    monkeypatch.setattr(direct_flow_module, "DirectAnswerFlow", capture_direct)
    monkeypatch.setattr(evidence_flow_module, "EvidenceAnswerFlow", capture_evidence)
    monkeypatch.setattr(embedder_module, "GeminiQueryEmbedder", lambda: object())
    monkeypatch.setattr(
        article_search_module,
        "PgVectorArticleSearchRepository",
        lambda *_args: object(),
    )
    monkeypatch.setattr(
        internal_search_module,
        "InternalSearchService",
        lambda **_kwargs: internal_search,
    )
    monkeypatch.setattr(
        planning_service_module,
        "QuestionPlanningService",
        lambda **_kwargs: object(),
    )
    delta_reporter = object()
    continuation = object()

    phases = composition._build_answering_phases(
        session_factory=cast(async_sessionmaker[AsyncSession], object()),
        delta_reporter=delta_reporter,
        continuation=continuation,
    )

    assert captured["direct"]["delta_reporter"] is delta_reporter
    assert captured["direct"]["continuation"] is continuation
    assert captured["direct"]["agent"] is DIRECT_ANSWER_AGENT
    assert (
        captured["direct"]["runtime_scope_factory"]
        is composition.activate_gemini_agent_runtime
    )
    assert captured["evidence"]["delta_reporter"] is delta_reporter
    assert captured["evidence"]["continuation"] is continuation
    assert captured["evidence"]["agent"] is EVIDENCE_ANSWER_AGENT
    assert (
        captured["evidence"]["runtime_scope_factory"]
        is composition.activate_gemini_agent_runtime
    )
    assert isinstance(phases, AnsweringPhases)
    assert phases.internal_search is internal_search
    assert phases.external_runtime_factory is external_runtime_factory
    assert phases.direct_answerer is not None
    assert phases.evidence_answerer is not None


def test_worker_imports_generation_stopped_from_shared_agent_contract() -> None:
    tree = ast.parse(inspect.getsource(agent_run_tasks))
    imports = {
        node.module: {alias.name for alias in node.names}
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert "AnswerGenerationStopped" in imports.get("app.agent.contract", set())
    assert "AnswerGenerationStopped" not in imports.get(
        "app.agent.answering.direct_answer.contract",
        set(),
    )
    assert agent_run_tasks.AnswerGenerationStopped is AnswerGenerationStopped


def test_worker_owns_only_answering_runner_boundary_for_semantic_execution() -> None:
    tree = ast.parse(inspect.getsource(agent_run_tasks))
    imports: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            imports.setdefault(node.module, set()).update(
                alias.name for alias in node.names
            )
    loaded_names = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    function_names = {
        node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }
    old_semantic_owners = {
        "AnswerQuestionInput",
        "QuestionAnsweringAgent",
        "QuestionAnsweringOrchestrator",
        "QuestionContextService",
        "QuestionResolvedEvent",
        "_latest_assistant_answer",
        "build_question_answering_starting_agent",
        "build_question_answering_agent",
        "build_question_context_generator",
        "make_safe_async_client",
        "starting_agent",
    }
    imported_names = {
        imported_name
        for module_names in imports.values()
        for imported_name in module_names
    }

    assert (
        {"build_answering_runner"} == imports.get("app.agent.composition", set()),
        {"QuestionResolvedRunHooks", "RunContext", "RunInput"}
        <= imports.get("app.agent.running", set()),
        old_semantic_owners.isdisjoint(imported_names),
        old_semantic_owners.isdisjoint(loaded_names),
        "_latest_assistant_answer" not in function_names,
    ) == (True, True, True, True, True)


def test_worker_keeps_question_context_history_bounded_to_six_messages() -> None:
    tree = ast.parse(inspect.getsource(agent_run_tasks._read_history))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    history_reads = [
        call
        for call in calls
        if isinstance(call.func, ast.Attribute)
        and call.func.attr == "read_recent_messages_before"
    ]

    assert agent_run_tasks.HISTORY_MESSAGE_LIMIT == 6
    assert len(history_reads) == 1
    limit = next(
        keyword.value for keyword in history_reads[0].keywords if keyword.arg == "limit"
    )
    assert isinstance(limit, ast.Name)
    assert limit.id == "HISTORY_MESSAGE_LIMIT"


async def _create_thread_message_run(
    session: AsyncSession,
    *,
    status: str = "queued",
    question: str = "worker question",
    history: list[tuple[str, str] | tuple[str, str, list[object]]] | None = None,
    created_at: datetime | None = None,
    started_at: datetime | None = None,
    attempt_epoch: int | None = None,
    progress_stage: str | None = None,
    error_code: str | None = None,
    user_id: str = TEST_USER_ID,
    quota_usage_date: date | None = None,
) -> tuple[AgentThread, AgentMessage, AgentRun]:
    thread = AgentThread(
        user_id=UUID(user_id),
        title="thread",
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    session.add(thread)
    await session.flush()
    history = history or []
    for seq, entry in enumerate(history, start=1):
        role, content, *missing_aspects = entry
        session.add(
            AgentMessage(
                thread_id=thread.id,
                seq=seq,
                role=role,
                content=content,
                missing_aspects=missing_aspects[0] if missing_aspects else [],
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
        progress_stage=progress_stage,
        error_code=error_code,
        quota_usage_date=quota_usage_date,
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
async def test_run_agent_answer_completes_run_and_persists_assistant_message(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        thread, _message, run = await _create_thread_message_run(session)
    fake_agent = FakeAgent(_external_result())
    persisted_results: list[AnswerQuestionResult] = []
    completed_epochs: list[int] = []
    original_complete = AgentRunRepository.complete_run

    async def capture_completed_result(
        repository: AgentRunRepository,
        *,
        run_id: UUID,
        result: AnswerQuestionResult,
        expected_attempt_epoch: int,
    ) -> bool:
        persisted_results.append(result)
        completed_epochs.append(expected_attempt_epoch)
        return await original_complete(
            repository,
            run_id=run_id,
            result=result,
            expected_attempt_epoch=expected_attempt_epoch,
        )

    answering_runner = _patch_worker_execution(
        monkeypatch,
        lambda **_kwargs: fake_agent,
    )
    monkeypatch.setattr(
        AgentRunRepository,
        "complete_run",
        capture_completed_result,
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
    assert answering_runner.calls[0].input.question == "worker question"
    assert persisted_results == [fake_agent.result]
    assert completed_epochs == [1]
    assert persisted_results[0] is fake_agent.result


@pytest.mark.asyncio
async def test_answering_runner_completes_follow_up_with_saved_history(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_question = "量子計算市場の現状を教えて"
    follow_up_question = "前回不足した企業比較を追加して"
    saved_gap = "主要企業ごとの売上比較"
    first_answer = "市場の概況は確認できました。"
    follow_up_answer = "主要企業の比較を追加しました。"

    async with session_factory() as session:
        thread, _message, follow_up_run = await _create_thread_message_run(
            session,
            question=follow_up_question,
            history=[
                ("user", first_question),
                ("assistant", first_answer, [saved_gap]),
            ],
        )
    thread_id = thread.id
    follow_up_run_id = follow_up_run.id

    runner_execution = FakeAgent(_direct_result(follow_up_answer))
    answering_runner = FakeAnsweringRunner(
        question_context=QuestionContext(
            standalone_question="量子計算市場の主要企業を比較して",
            content_requirements=[
                AnswerRequirement(requirement_id="c1", description=saved_gap)
            ],
            relevant_prior_coverage=first_answer,
        ),
        previous_answer=first_answer,
    )
    _patch_worker_execution(
        monkeypatch,
        lambda **_kwargs: runner_execution,
        answering_runner=answering_runner,
    )

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=follow_up_run_id),
        ctx=_ctx(session_factory),
    )

    async with session_factory() as session:
        completed = await session.get(AgentRun, follow_up_run_id)
        assert completed is not None
        assert completed.assistant_message_id is not None
        assistant = await session.get(
            AgentMessage,
            completed.assistant_message_id,
        )
        assert assistant is not None

    assert completed.status == "completed"
    assert (
        assistant.thread_id,
        assistant.seq,
        assistant.role,
        assistant.content,
        assistant.missing_aspects,
    ) == (thread_id, 4, "assistant", follow_up_answer, [])
    assert answering_runner.calls[0].input.history == (
        ThreadMessageSnapshot(role="user", content=first_question),
        ThreadMessageSnapshot(
            role="assistant",
            content=first_answer,
            missing_aspects=(saved_gap,),
        ),
    )
    assert len(runner_execution.calls) == 1
    assert (
        runner_execution.calls[0].question_context.standalone_question,
        runner_execution.calls[0].previous_answer,
    ) == (
        "量子計算市場の主要企業を比較して",
        first_answer,
    )


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

    _patch_worker_execution(monkeypatch, build_agent)
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
    _patch_worker_execution(monkeypatch, build_agent)
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

    _patch_worker_execution(monkeypatch, build_agent)
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
async def test_run_agent_answer_binds_attempt_epoch_to_live_and_db_controls(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    fake_agent = FakeAgent(_direct_result())
    captured_kwargs: dict[str, object] = {}
    redis = object()
    FakeLiveStreamPublisher.instances = []
    CapturingDeltaReporter.instances = []
    CapturingExecutionProbe.instances = []
    CapturingProgressWriter.instances = []

    def build_agent(**kwargs: object) -> FakeAgent:
        captured_kwargs.update(kwargs)
        return fake_agent

    _patch_worker_execution(monkeypatch, build_agent)
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
    )
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveAnswerDeltaReporter",
        CapturingDeltaReporter,
        raising=False,
    )
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunExecutionProbe",
        CapturingExecutionProbe,
        raising=False,
    )
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunProgressWriter",
        CapturingProgressWriter,
        raising=False,
    )

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )

    assert len(CapturingDeltaReporter.instances) == 1
    assert len(CapturingExecutionProbe.instances) == 1
    assert len(CapturingProgressWriter.instances) == 1
    stream = FakeLiveStreamPublisher.instances[0]
    delta_reporter = CapturingDeltaReporter.instances[0]
    probe = CapturingExecutionProbe.instances[0]
    progress_writer = CapturingProgressWriter.instances[0]
    assert delta_reporter.publisher is stream
    assert delta_reporter.run_id == run.id
    assert delta_reporter.attempt_epoch == 1
    assert probe.session_factory is session_factory
    assert probe.run_id == run.id
    assert probe.attempt_epoch == 1
    assert progress_writer.session_factory is session_factory
    assert progress_writer.run_id == run.id
    assert progress_writer.attempt_epoch == 1
    assert captured_kwargs["delta_reporter"] is delta_reporter
    assert captured_kwargs["continuation"] is probe


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
    _patch_worker_execution(monkeypatch, lambda **_kwargs: fake_agent)
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
    CapturingDeltaReporter.instances = []
    CapturingExecutionProbe.instances = []

    def forbidden_builder(*_args: object, **_kwargs: object) -> None:
        pytest.fail(
            "acquire skip後にexecution dependencyをbuildしてはいけません",
        )

    monkeypatch.setattr(
        agent_run_tasks,
        "build_answering_runner",
        forbidden_builder,
        raising=False,
    )
    monkeypatch.setattr(
        agent_run_tasks,
        "get_redis",
        forbidden_builder,
    )
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveEventPublisher",
        ForbiddenConstruction,
    )
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveStreamPublisher",
        FakeLiveStreamPublisher,
        raising=False,
    )
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveAnswerDeltaReporter",
        ForbiddenConstruction,
        raising=False,
    )
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunExecutionProbe",
        ForbiddenConstruction,
        raising=False,
    )

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )

    assert FakeLiveStreamPublisher.instances == []
    assert CapturingDeltaReporter.instances == []
    assert CapturingExecutionProbe.instances == []


@pytest.mark.asyncio
async def test_run_agent_answer_passes_answering_runner_and_resolved_hook(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    question = "それの株価への影響は？"
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(
            session,
            question=question,
            history=[
                ("user", "bounded window外の質問"),
                ("assistant", "古い回答 [[0]]"),
                ("user", "NVIDIA の発表を説明して"),
                ("assistant", "中間回答 [[1]]"),
                ("user", "株価への影響も知りたい"),
                ("assistant", "前回の回答 [[2]]", ["保存済みの不足"]),
                ("user", "さらに詳しく"),
            ],
            attempt_epoch=4,
        )
    runner_execution = FakeAgent(_direct_result())
    answering_runner = FakeAnsweringRunner(
        question_context=QuestionContext(
            standalone_question="NVIDIA の発表が株価へ与える影響は？",
        )
    )
    runner_builder_calls: list[dict[str, object]] = []

    def build_runner_execution(**kwargs: object) -> FakeAgent:
        runner_builder_calls.append(kwargs)
        return runner_execution

    class FixedDateTime:
        calls = 0

        @classmethod
        def now(cls, timezone: object) -> datetime:
            assert timezone is UTC
            cls.calls += 1
            return datetime(2026, 7, 16, 9, 30, tzinfo=UTC)

    FakeLiveEventPublisher.instances = []
    FakeLiveStreamPublisher.instances = []
    _patch_worker_execution(
        monkeypatch,
        build_runner_execution,
        answering_runner=answering_runner,
    )

    def fail_if_legacy_semantic_owner_is_called(
        *_args: object,
        **_kwargs: object,
    ) -> None:
        pytest.fail("workerがAnsweringRunner境界ではなく旧semantic ownerを呼びました")

    for legacy_name in (
        "build_question_context_generator",
        "make_safe_async_client",
    ):
        monkeypatch.setattr(
            agent_run_tasks,
            legacy_name,
            fail_if_legacy_semantic_owner_is_called,
            raising=False,
        )
    monkeypatch.setattr(agent_run_tasks, "datetime", FixedDateTime)
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

    assert FixedDateTime.calls == 1
    assert len(answering_runner.calls) == 1
    answering_runner_call = answering_runner.calls[0]
    assert answering_runner_call.input.question == question
    assert isinstance(answering_runner_call.input.history, tuple)
    assert [
        (message.role, message.content, message.missing_aspects)
        for message in answering_runner_call.input.history
    ] == [
        ("assistant", "古い回答 [[0]]", ()),
        ("user", "NVIDIA の発表を説明して", ()),
        ("assistant", "中間回答 [[1]]", ()),
        ("user", "株価への影響も知りたい", ()),
        ("assistant", "前回の回答 [[2]]", ("保存済みの不足",)),
        ("user", "さらに詳しく", ()),
    ]
    assert answering_runner_call.run_context == RunContext(
        run_id=run.id,
        as_of=datetime(2026, 7, 16, 9, 30, tzinfo=UTC),
    )
    assert answering_runner_call.run_context.as_of.utcoffset() == timedelta(0)
    assert isinstance(answering_runner_call.hooks, QuestionResolvedRunHooks)
    assert len(runner_execution.calls) == 1
    assert runner_execution.calls[0].run_context.as_of == (
        answering_runner_call.run_context.as_of
    )
    assert len(runner_builder_calls) == 1
    runner_kwargs = runner_builder_calls[0]
    assert runner_kwargs["session_factory"] is session_factory
    assert isinstance(runner_kwargs["events"], AgentRunLiveActivityReporter)
    assert runner_kwargs["progress"] is not None
    assert runner_kwargs["delta_reporter"] is not None
    assert runner_kwargs["continuation"] is not None
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
    stream = FakeLiveStreamPublisher.instances[0]
    assert stream.run_id == run.id
    assert stream.attempt_epoch == 5


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

    _patch_worker_execution(monkeypatch, lambda **_kwargs: fake_agent)
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

    _patch_worker_execution(monkeypatch, lambda **_kwargs: fake_agent)
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
async def test_generation_stopped_is_routine_return_without_run_transition(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    fake_agent = FakeAgent(exc=AnswerGenerationStopped())
    complete_calls: list[tuple[UUID, int]] = []
    mark_failed_calls: list[tuple[UUID, int]] = []

    async def observe_complete(
        _repository: AgentRunRepository,
        *,
        run_id: UUID,
        result: AnswerQuestionResult,
        expected_attempt_epoch: int,
    ) -> bool:
        assert result == fake_agent.result
        complete_calls.append((run_id, expected_attempt_epoch))
        return False

    async def observe_mark_failed(
        _repository: AgentRunRepository,
        run_id: UUID,
        *,
        error_code: agent_run_tasks.AgentRunErrorCode,
        expected_attempt_epoch: int,
    ) -> bool:
        assert error_code == agent_run_tasks.AgentRunErrorCode.INTERNAL_ERROR
        mark_failed_calls.append((run_id, expected_attempt_epoch))
        return False

    _patch_delta_worker(
        monkeypatch,
        lambda **_kwargs: fake_agent,
    )
    monkeypatch.setattr(AgentRunRepository, "complete_run", observe_complete)
    monkeypatch.setattr(AgentRunRepository, "mark_failed", observe_mark_failed)

    with capture_logs() as logs:
        await agent_run_tasks.run_agent_answer(
            trigger=AgentRunTrigger(run_id=run.id),
            ctx=_ctx(session_factory),
        )

    assert complete_calls == []
    assert mark_failed_calls == []
    stop_logs = [
        entry for entry in logs if entry.get("event") == "agent_run_generation_stopped"
    ]
    assert len(stop_logs) == 1
    assert stop_logs[0]["log_level"] == "info"
    assert stop_logs[0]["run_id"] == str(run.id)
    assert not any(
        entry.get("event")
        in {"agent_run_generation_unavailable", "agent_run_unexpected_error"}
        for entry in logs
    )
    stream = FakeLiveStreamPublisher.instances[0]
    assert not any(
        isinstance(event, AgentRunLiveStreamTerminalEvent) for event in stream.published
    )
    async with session_factory() as session:
        persisted = await session.get(AgentRun, run.id)
        assert persisted is not None
        assert persisted.status == "running"
        assert persisted.attempt_epoch == 1
        messages = (
            (
                await session.execute(
                    select(AgentMessage).where(
                        AgentMessage.thread_id == persisted.thread_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [message.role for message in messages] == ["user"]


@pytest.mark.asyncio
async def test_epoch_advance_stops_old_worker_through_actual_probe(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)

    class ManualClock:
        def __init__(self) -> None:
            self.now = 0.0

        def __call__(self) -> float:
            return self.now

    clock = ManualClock()
    production_probe_type = agent_run_tasks.AgentRunExecutionProbe
    probe_bindings: list[tuple[object, UUID, int]] = []
    complete_calls: list[tuple[UUID, int]] = []
    mark_failed_calls: list[tuple[UUID, int]] = []

    def build_probe(
        bound_session_factory: object,
        run_id: UUID,
        attempt_epoch: int,
    ) -> object:
        probe_bindings.append((bound_session_factory, run_id, attempt_epoch))
        return production_probe_type(
            bound_session_factory,
            run_id,
            attempt_epoch,
            clock=clock,
        )

    class EpochAdvancingAgent:
        def __init__(self) -> None:
            self.continuation: object | None = None

        async def answer(self, _input: AnsweringRunContext) -> AnswerQuestionResult:
            assert self.continuation is not None
            assert await self.continuation.should_continue() is True  # type: ignore[attr-defined]
            async with session_factory() as reacquire_session:
                async with reacquire_session.begin():
                    prepared = await AgentRunRepository(
                        reacquire_session
                    ).acquire_for_execution(run.id)
            assert prepared is not None
            assert prepared.attempt_epoch == 2
            clock.now = 2.0
            assert await self.continuation.should_continue() is False  # type: ignore[attr-defined]
            raise AnswerGenerationStopped

    fake_agent = EpochAdvancingAgent()

    def build_agent(**kwargs: object) -> EpochAdvancingAgent:
        fake_agent.continuation = kwargs["continuation"]
        return fake_agent

    async def observe_complete(
        _repository: AgentRunRepository,
        *,
        run_id: UUID,
        result: AnswerQuestionResult,
        expected_attempt_epoch: int,
    ) -> bool:
        assert result == fake_agent.result
        complete_calls.append((run_id, expected_attempt_epoch))
        return False

    async def observe_mark_failed(
        _repository: AgentRunRepository,
        run_id: UUID,
        *,
        error_code: agent_run_tasks.AgentRunErrorCode,
        expected_attempt_epoch: int,
    ) -> bool:
        assert error_code == agent_run_tasks.AgentRunErrorCode.INTERNAL_ERROR
        mark_failed_calls.append((run_id, expected_attempt_epoch))
        return False

    _patch_delta_worker(monkeypatch, build_agent)
    monkeypatch.setattr(agent_run_tasks, "AgentRunExecutionProbe", build_probe)
    monkeypatch.setattr(AgentRunRepository, "complete_run", observe_complete)
    monkeypatch.setattr(AgentRunRepository, "mark_failed", observe_mark_failed)

    with capture_logs() as logs:
        await agent_run_tasks.run_agent_answer(
            trigger=AgentRunTrigger(run_id=run.id),
            ctx=_ctx(session_factory),
        )

    assert probe_bindings == [(session_factory, run.id, 1)]
    assert complete_calls == []
    assert mark_failed_calls == []
    stop_logs = [
        entry for entry in logs if entry.get("event") == "agent_run_generation_stopped"
    ]
    assert len(stop_logs) == 1
    assert stop_logs[0]["log_level"] == "info"
    assert stop_logs[0]["run_id"] == str(run.id)
    assert not any(
        entry.get("event")
        in {"agent_run_generation_unavailable", "agent_run_unexpected_error"}
        for entry in logs
    )
    assert not any(
        isinstance(event, AgentRunLiveStreamTerminalEvent)
        for event in FakeLiveStreamPublisher.instances[0].published
    )
    async with session_factory() as session:
        persisted = await session.get(AgentRun, run.id)
        assert persisted is not None
        assert persisted.status == "running"
        assert persisted.attempt_epoch == 2
        assert persisted.assistant_message_id is None
        messages = (
            (
                await session.execute(
                    select(AgentMessage).where(
                        AgentMessage.thread_id == persisted.thread_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [message.role for message in messages] == ["user"]


@pytest.mark.asyncio
async def test_delta_finish_precedes_completed_commit_and_terminal(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    order: list[str] = []
    fake_agent = DeltaReportingAgent(
        result=_direct_result(),
        fragments=["D" * 512],
        order=order,
    )
    original_complete = AgentRunRepository.complete_run

    def build_agent(**kwargs: object) -> DeltaReportingAgent:
        fake_agent.delta_reporter = kwargs["delta_reporter"]
        assert kwargs["continuation"] is not None
        return fake_agent

    async def observe_complete(
        repository: AgentRunRepository,
        *,
        run_id: UUID,
        result: AnswerQuestionResult,
        expected_attempt_epoch: int,
    ) -> bool:
        order.append("complete_start")
        assert expected_attempt_epoch == 1
        return await original_complete(
            repository,
            run_id=run_id,
            result=result,
            expected_attempt_epoch=expected_attempt_epoch,
        )

    _patch_delta_worker(monkeypatch, build_agent)
    monkeypatch.setattr(AgentRunRepository, "complete_run", observe_complete)

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )

    assert order == ["delta_finish", "complete_start"]
    stream = FakeLiveStreamPublisher.instances[0]
    assert [type(event) for event in stream.published] == [
        AgentRunLiveStreamAnswerDeltaEvent,
        AgentRunLiveStreamTerminalEvent,
    ]
    assert stream.published[-1] == AgentRunLiveStreamTerminalEvent(status="completed")
    async with session_factory() as session:
        persisted = await session.get(AgentRun, run.id)
        assert persisted is not None
        assert persisted.status == "completed"
        assert persisted.assistant_message_id is not None


@pytest.mark.asyncio
async def test_evidence_revision_events_precede_persisted_completed_terminal(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    result = _external_result()
    visible_revision = "外部根拠つき回答。"
    fake_agent = RevisionReportingAgent(result, visible_revision)
    captured_controls: dict[str, object] = {}

    def build_agent(**kwargs: object) -> RevisionReportingAgent:
        captured_controls.update(kwargs)
        fake_agent.delta_reporter = kwargs["delta_reporter"]
        fake_agent.continuation = kwargs["continuation"]
        return fake_agent

    class CommitCheckingPublisher(FakeLiveStreamPublisher):
        async def publish(self, event: object) -> str | None:
            if isinstance(event, AgentRunLiveStreamTerminalEvent):
                async with session_factory() as session:
                    persisted = await session.get(AgentRun, run.id)
                    assert persisted is not None
                    assert persisted.status == "completed"
                    assert persisted.assistant_message_id is not None
                    assistant = await session.get(
                        AgentMessage,
                        persisted.assistant_message_id,
                    )
                    assert assistant is not None
                    assert assistant.role == "assistant"
                    assert assistant.content == result.answer
            return await super().publish(event)

    _patch_delta_worker(
        monkeypatch,
        build_agent,
        stream_publisher=CommitCheckingPublisher,
    )

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )

    stream = FakeLiveStreamPublisher.instances[0]
    assert stream.published == [
        AgentRunLiveStreamAnswerResetEvent(generation=2),
        AgentRunLiveStreamAnswerDeltaEvent(
            generation=2,
            text=visible_revision,
        ),
        AgentRunLiveStreamTerminalEvent(status="completed"),
    ]
    assert captured_controls["delta_reporter"] is fake_agent.delta_reporter
    assert captured_controls["continuation"] is fake_agent.continuation


@pytest.mark.asyncio
async def test_delta_breaker_open_does_not_block_final_commit_or_terminal(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    fake_agent = DeltaReportingAgent(
        result=_direct_result(),
        fragments=["A" * 512, "B" * 512, "C" * 512, "D" * 512],
    )

    def build_agent(**kwargs: object) -> DeltaReportingAgent:
        fake_agent.delta_reporter = kwargs["delta_reporter"]
        return fake_agent

    _patch_delta_worker(monkeypatch, build_agent)
    monkeypatch.setattr(
        FakeLiveStreamPublisher,
        "publish_outcomes",
        [None, None, None, "terminal-0"],
    )

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )

    stream = FakeLiveStreamPublisher.instances[0]
    deltas = [
        event
        for event in stream.published
        if isinstance(event, AgentRunLiveStreamAnswerDeltaEvent)
    ]
    terminals = [
        event
        for event in stream.published
        if isinstance(event, AgentRunLiveStreamTerminalEvent)
    ]
    assert len(deltas) == 3
    assert terminals == [AgentRunLiveStreamTerminalEvent(status="completed")]
    async with session_factory() as session:
        persisted = await session.get(AgentRun, run.id)
        assert persisted is not None
        assert persisted.status == "completed"
        assert persisted.assistant_message_id is not None


@pytest.mark.asyncio
async def test_provider_failure_after_delta_commits_failed_without_assistant(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    fake_agent = DeltaReportingAgent(
        exc=AIProviderError(),
        fragments=["P" * 512],
        finish=False,
    )

    def build_agent(**kwargs: object) -> DeltaReportingAgent:
        fake_agent.delta_reporter = kwargs["delta_reporter"]
        return fake_agent

    _patch_delta_worker(monkeypatch, build_agent)

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )

    stream = FakeLiveStreamPublisher.instances[0]
    assert [type(event) for event in stream.published] == [
        AgentRunLiveStreamAnswerDeltaEvent,
        AgentRunLiveStreamTerminalEvent,
    ]
    assert stream.published[-1] == AgentRunLiveStreamTerminalEvent(
        status="failed",
        errorCode="generation_unavailable",
    )
    async with session_factory() as session:
        persisted = await session.get(AgentRun, run.id)
        assert persisted is not None
        assert persisted.status == "failed"
        assert persisted.error_code == "generation_unavailable"
        assert persisted.assistant_message_id is None
        messages = (
            (
                await session.execute(
                    select(AgentMessage).where(
                        AgentMessage.thread_id == persisted.thread_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [message.role for message in messages] == ["user"]


@pytest.mark.parametrize("completion_outcome", ["lost", "skipped"])
@pytest.mark.asyncio
async def test_completion_loser_with_existing_delta_has_no_terminal_or_assistant(
    completion_outcome: str,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    fake_agent = DeltaReportingAgent(
        result=_direct_result(),
        fragments=["L" * 512],
    )

    def build_agent(**kwargs: object) -> DeltaReportingAgent:
        fake_agent.delta_reporter = kwargs["delta_reporter"]
        return fake_agent

    async def lose_or_skip_completion(
        _repository: AgentRunRepository,
        *,
        run_id: UUID,
        result: AnswerQuestionResult,
        expected_attempt_epoch: int,
    ) -> bool:
        assert (run_id, result, expected_attempt_epoch) == (
            run.id,
            fake_agent.result,
            1,
        )
        if completion_outcome == "lost":
            raise RunTransitionLostError
        return False

    _patch_delta_worker(monkeypatch, build_agent)
    monkeypatch.setattr(
        AgentRunRepository,
        "complete_run",
        lose_or_skip_completion,
    )

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )

    stream = FakeLiveStreamPublisher.instances[0]
    assert (
        len(
            [
                event
                for event in stream.published
                if isinstance(event, AgentRunLiveStreamAnswerDeltaEvent)
            ]
        )
        == 1
    )
    assert not any(
        isinstance(event, AgentRunLiveStreamTerminalEvent) for event in stream.published
    )
    async with session_factory() as session:
        persisted = await session.get(AgentRun, run.id)
        assert persisted is not None
        assert persisted.status == "running"
        assert persisted.assistant_message_id is None
        messages = (
            (
                await session.execute(
                    select(AgentMessage).where(
                        AgentMessage.thread_id == persisted.thread_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [message.role for message in messages] == ["user"]


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
        *,
        run_id: UUID,
        result: AnswerQuestionResult,
        expected_attempt_epoch: int,
    ) -> bool:
        assert (run_id, result, expected_attempt_epoch) == (
            run.id,
            fake_agent.result,
            1,
        )
        raise RuntimeError("completion failed")

    _patch_worker_execution(monkeypatch, lambda **_kwargs: fake_agent)
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
        *,
        run_id: UUID,
        result: AnswerQuestionResult,
        expected_attempt_epoch: int,
    ) -> bool:
        assert (run_id, result, expected_attempt_epoch) == (
            run.id,
            fake_agent.result,
            1,
        )
        raise RunTransitionLostError

    _patch_worker_execution(monkeypatch, lambda **_kwargs: fake_agent)
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
        *,
        run_id: UUID,
        result: AnswerQuestionResult,
        expected_attempt_epoch: int,
    ) -> bool:
        assert (run_id, result, expected_attempt_epoch) == (
            run.id,
            fake_agent.result,
            1,
        )
        return False

    _patch_worker_execution(monkeypatch, lambda **_kwargs: fake_agent)
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
        run_id: UUID,
        *,
        error_code: agent_run_tasks.AgentRunErrorCode,
        expected_attempt_epoch: int,
    ) -> bool:
        assert (run_id, error_code, expected_attempt_epoch) == (
            run.id,
            agent_run_tasks.AgentRunErrorCode.GENERATION_UNAVAILABLE,
            1,
        )
        return False

    _patch_worker_execution(monkeypatch, lambda **_kwargs: fake_agent)
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

    _patch_worker_execution(monkeypatch, build_agent)
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
    "question_context",
    [
        QuestionContext(standalone_question="それの株価への影響は？"),
        QuestionContext(
            standalone_question="それの株価への影響は？",
            content_requirements=[
                {
                    "requirement_id": "c1",
                    "description": "それの株価への影響は？",
                }
            ],
        ),
    ],
    ids=("echo", "safe-fallback"),
)
async def test_run_agent_answer_does_not_publish_echo_or_fallback_question_context(
    question_context: QuestionContext,
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
    answering_runner = FakeAnsweringRunner(question_context=question_context)
    FakeLiveEventPublisher.instances = []
    _patch_worker_execution(
        monkeypatch,
        lambda **_kwargs: fake_agent,
        answering_runner=answering_runner,
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

    assert len(fake_agent.calls) == 1
    assert fake_agent.calls[0].question_context.standalone_question == question
    assert answering_runner.calls[0].input.history == (
        ThreadMessageSnapshot(role="assistant", content="前回の回答"),
    )
    assert FakeLiveEventPublisher.instances[0].events == []


@pytest.mark.asyncio
async def test_initial_question_does_not_publish_resolved_event(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    question = "NVIDIA の直近発表は？"
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(
            session,
            question=question,
        )
    fake_agent = FakeAgent(_direct_result())
    answering_runner = FakeAnsweringRunner(
        question_context=QuestionContext(standalone_question="書き換えた質問")
    )
    FakeLiveEventPublisher.instances = []
    _patch_worker_execution(
        monkeypatch,
        lambda **_kwargs: fake_agent,
        answering_runner=answering_runner,
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

    assert len(answering_runner.calls) == 1
    assert answering_runner.calls[0].input == RunInput(question=question, history=())
    assert len(fake_agent.calls) == 1
    assert FakeLiveEventPublisher.instances[0].events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [AIProviderConfigurationError(), AIProviderError()])
async def test_answering_runner_setup_error_marks_generation_unavailable(
    exc: Exception,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    question = "NVIDIA の直近発表は？"
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(
            session,
            question=question,
            history=[("assistant", "前回の回答")],
        )
    fake_agent = FakeAgent(_direct_result())
    answering_runner = FakeAnsweringRunner(exc=exc)
    FakeLiveEventPublisher.instances = []
    _patch_worker_execution(
        monkeypatch,
        lambda **_kwargs: fake_agent,
        answering_runner=answering_runner,
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

    assert len(answering_runner.calls) == 1
    assert fake_agent.calls == []
    async with session_factory() as session:
        failed = await session.get(AgentRun, run.id)
        assert failed is not None
        assert failed.status == "failed"
        assert failed.error_code == "generation_unavailable"


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
                    expected_attempt_epoch=run.attempt_epoch,
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
    [
        AIProviderConfigurationError(),
        AIProviderError("SHOULD_NOT_LEAK"),
        DirectAnswerInvalidError(),
    ],
    ids=("configuration", "provider", "direct-draft"),
)
async def test_run_agent_answer_generation_error_marks_failed_without_leaking_message(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    generation_error: Exception,
) -> None:
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    fake_agent = FakeAgent(exc=generation_error)
    _patch_worker_execution(monkeypatch, lambda **_kwargs: fake_agent)

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

    _patch_worker_execution(monkeypatch, build_agent)

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
async def test_answering_runner_failure_does_not_execute_answering_workflow(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    fake_agent = FakeAgent(_direct_result())
    error = AIProviderConfigurationError()
    answering_runner = FakeAnsweringRunner(exc=error)
    _patch_worker_execution(
        monkeypatch,
        lambda **_kwargs: fake_agent,
        answering_runner=answering_runner,
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
        assert failed.progress_stage is None
    assert len(answering_runner.calls) == 1
    assert fake_agent.calls == []


@pytest.mark.asyncio
async def test_run_agent_answer_unexpected_error_marks_internal_error(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    fake_agent = FakeAgent(exc=RuntimeError("SHOULD_NOT_LEAK"))
    FakeLiveStreamPublisher.instances = []
    _patch_worker_execution(monkeypatch, lambda **_kwargs: fake_agent)
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

    with capture_logs() as logs:
        await agent_run_tasks.run_agent_answer(
            trigger=AgentRunTrigger(run_id=run.id),
            ctx=_ctx(session_factory),
        )

    unexpected_logs = [
        entry for entry in logs if entry.get("event") == "agent_run_unexpected_error"
    ]
    assert len(unexpected_logs) == 1
    assert unexpected_logs[0]["log_level"] == "error"
    assert unexpected_logs[0]["error_type"] == "RuntimeError"
    assert "exception" not in unexpected_logs[0]
    assert "exc_info" not in unexpected_logs[0]
    assert "SHOULD_NOT_LEAK" not in repr(unexpected_logs[0])

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
            setup_session,
            status="running",
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

        with pytest.raises(RunTransitionLostError):
            async with stale_session.begin():
                await AgentRunRepository(stale_session).complete_run(
                    run_id=run.id,
                    result=_direct_result(),
                    expected_attempt_epoch=stale_run.attempt_epoch,
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
        )
    stale_session = session_factory()
    try:
        async with stale_session.begin():
            stale_run = await stale_session.get(AgentRun, run.id)
            assert stale_run is not None
            assert stale_run.attempt_epoch == 1

        async with session_factory() as winner_session:
            async with winner_session.begin():
                prepared = await AgentRunRepository(
                    winner_session
                ).acquire_for_execution(run.id)
                assert prepared is not None
                assert prepared.attempt_epoch == 2

        with pytest.raises(RunTransitionLostError):
            async with stale_session.begin():
                await AgentRunRepository(stale_session).complete_run(
                    run_id=run.id,
                    result=_external_result(),
                    expected_attempt_epoch=stale_run.attempt_epoch,
                )
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
            prepared = await AgentRunRepository(session).acquire_for_execution(run.id)
            assert prepared is not None
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
            prepared.attempt_epoch,
            None,
        )


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
            progress_stage="synthesizing",
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
        assert (
            reacquired.status,
            reacquired.started_at,
            reacquired.attempt_epoch,
            reacquired.progress_stage,
        ) == ("running", now, 2, None)
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
                    expected_attempt_epoch=run.attempt_epoch,
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
async def test_mark_enqueue_failed_remains_epoch_independent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)
    async with session_factory() as setup_session:
        _thread, _message, run = await _create_thread_message_run(
            setup_session,
            attempt_epoch=7,
        )

    async with session_factory() as session:
        async with session.begin():
            transitioned = await AgentRunRepository(session).mark_enqueue_failed(
                run.id,
                now=now,
            )

    assert transitioned is True
    async with session_factory() as session:
        failed = await session.get(AgentRun, run.id)
        assert failed is not None
        assert (
            failed.status,
            failed.error_code,
            failed.attempt_epoch,
            failed.completed_at,
        ) == ("failed", "enqueue_failed", 7, now)


@pytest.mark.asyncio
async def test_sweep_stale_agent_runs_marks_only_old_active_runs(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)
    async with session_factory() as session:
        _t1, _m1, old_queued = await _create_thread_message_run(
            session,
            created_at=now - timedelta(minutes=21),
            quota_usage_date=date(2026, 7, 9),
        )
        _t2, _m2, old_running = await _create_thread_message_run(
            session,
            status="running",
            created_at=now - timedelta(minutes=30),
            started_at=now - timedelta(minutes=21),
            attempt_epoch=7,
            quota_usage_date=date(2026, 7, 9),
        )
        _t3, _m3, old_legacy = await _create_thread_message_run(
            session,
            created_at=now - timedelta(minutes=22),
        )
        _t4, _m4, fresh = await _create_thread_message_run(
            session, created_at=now - timedelta(minutes=19)
        )
        _t5, _m5, terminal = await _create_thread_message_run(
            session,
            status="failed",
            created_at=now - timedelta(minutes=30),
            error_code="internal_error",
        )
        result = await AgentRunRepository(session).sweep_stale_runs(now=now)
        await session.commit()
        assert (
            result.total_count,
            result.quota_queued_count,
            result.quota_running_count,
        ) == (3, 1, 1)

    async with session_factory() as session:
        swept_queued = await session.get(AgentRun, old_queued.id)
        swept_running = await session.get(AgentRun, old_running.id)
        swept_legacy = await session.get(AgentRun, old_legacy.id)
        untouched_fresh = await session.get(AgentRun, fresh.id)
        untouched_terminal = await session.get(AgentRun, terminal.id)
        assert swept_queued is not None
        assert swept_running is not None
        assert swept_legacy is not None
        assert untouched_fresh is not None
        assert untouched_terminal is not None
        assert swept_queued.status == "failed"
        assert (swept_running.attempt_epoch, swept_running.error_code) == (7, "stale")
        assert swept_legacy.status == "failed"
        assert untouched_fresh.status == "queued"
        assert untouched_terminal.error_code == "internal_error"


@pytest.mark.asyncio
async def test_sweep_task_observes_quota_stale_runs_only_after_commit(
    session_factory: async_sessionmaker[AsyncSession],
    capfire: CaptureLogfire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    usage_date = date(2026, 7, 20)
    async with session_factory() as session:
        _t1, _m1, old_queued = await _create_thread_message_run(
            session,
            question="sensitive queued question",
            created_at=now - timedelta(minutes=21),
            quota_usage_date=usage_date,
        )
        _t2, _m2, old_running = await _create_thread_message_run(
            session,
            question="sensitive running question",
            status="running",
            created_at=now - timedelta(minutes=30),
            started_at=now - timedelta(minutes=21),
            quota_usage_date=usage_date,
        )
        _t3, _m3, old_legacy = await _create_thread_message_run(
            session,
            question="sensitive legacy question",
            created_at=now - timedelta(minutes=22),
        )

    observed: list[dict[str, int]] = []
    original_observer = daily_quota_observability.observe_stale_reservations

    def observe_stale_reservations(*, queued_count: int, running_count: int) -> None:
        observed.append(
            {
                "queued_count": queued_count,
                "running_count": running_count,
            }
        )
        original_observer(
            queued_count=queued_count,
            running_count=running_count,
        )

    monkeypatch.setattr(
        daily_quota_observability,
        "observe_stale_reservations",
        observe_stale_reservations,
    )

    with capture_logs() as logs:
        await agent_run_tasks.sweep_stale_agent_runs(ctx=_ctx(session_factory))

    assert [
        entry for entry in logs if entry.get("event") == "agent_runs_stale_swept"
    ] == [{"count": 3, "event": "agent_runs_stale_swept", "log_level": "info"}]
    assert [
        entry
        for entry in logs
        if entry.get("event") == "agent_user_daily_quota_stale_reservations_retained"
    ] == [
        {
            "queued_count": 1,
            "running_count": 1,
            "event": "agent_user_daily_quota_stale_reservations_retained",
            "log_level": "warning",
        }
    ]
    assert {
        (point["value"], frozenset(point.get("attributes", {}).items()))
        for point in _quota_stale_metric_points(capfire)
    } == {
        (1, frozenset({("previous_status", "queued")})),
        (1, frozenset({("previous_status", "running")})),
    }
    assert observed == [{"queued_count": 1, "running_count": 1}]

    async with session_factory() as session:
        statuses = [
            await session.get(AgentRun, run_id)
            for run_id in (old_queued.id, old_running.id, old_legacy.id)
        ]
    assert [run.status if run is not None else None for run in statuses] == [
        "failed",
        "failed",
        "failed",
    ]


@pytest.mark.asyncio
async def test_sweep_task_legacy_stale_batch_emits_no_quota_alert_or_metric(
    session_factory: async_sessionmaker[AsyncSession],
    capfire: CaptureLogfire,
) -> None:
    async with session_factory() as session:
        _thread, _message, legacy = await _create_thread_message_run(
            session,
            question="sensitive legacy-only question",
            created_at=datetime.now(UTC) - timedelta(minutes=21),
        )

    with capture_logs() as logs:
        await agent_run_tasks.sweep_stale_agent_runs(ctx=_ctx(session_factory))

    assert [
        entry for entry in logs if entry.get("event") == "agent_runs_stale_swept"
    ] == [{"count": 1, "event": "agent_runs_stale_swept", "log_level": "info"}]
    assert not [
        entry
        for entry in logs
        if entry.get("event") == "agent_user_daily_quota_stale_reservations_retained"
    ]
    assert _quota_stale_metric_points(capfire) == []

    async with session_factory() as session:
        swept = await session.get(AgentRun, legacy.id)
    assert swept is not None and swept.status == "failed"


@pytest.mark.asyncio
async def test_empty_sweep_emits_total_only_without_quota_alert_or_metric(
    session_factory: async_sessionmaker[AsyncSession],
    capfire: CaptureLogfire,
) -> None:
    with capture_logs() as logs:
        await agent_run_tasks.sweep_stale_agent_runs(ctx=_ctx(session_factory))

    assert [
        entry for entry in logs if entry.get("event") == "agent_runs_stale_swept"
    ] == [{"count": 0, "event": "agent_runs_stale_swept", "log_level": "info"}]
    assert not [
        entry
        for entry in logs
        if entry.get("event") == "agent_user_daily_quota_stale_reservations_retained"
    ]
    assert _quota_stale_metric_points(capfire) == []


@pytest.mark.asyncio
async def test_sweep_task_does_not_observe_quota_results_when_transaction_rolls_back(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as setup_session:
        _thread, _message, stale_run = await _create_thread_message_run(
            setup_session,
            created_at=datetime.now(UTC) - timedelta(minutes=21),
            quota_usage_date=date(2026, 7, 20),
        )

    calls: list[dict[str, int]] = []
    commit_attempted = False

    def observe_stale_reservations(*, queued_count: int, running_count: int) -> None:
        calls.append(
            {
                "queued_count": queued_count,
                "running_count": running_count,
            }
        )

    def fail_commit(_session: object) -> None:
        nonlocal commit_attempted
        commit_attempted = True
        raise RuntimeError("sweep commit failure")

    monkeypatch.setattr(
        daily_quota_observability,
        "observe_stale_reservations",
        observe_stale_reservations,
    )
    failing_session = session_factory()
    sa_event.listen(
        failing_session.sync_session,
        "before_commit",
        fail_commit,
        once=True,
    )

    def failing_session_factory() -> AsyncSession:
        return failing_session

    with (
        capture_logs() as logs,
        pytest.raises(RuntimeError, match="sweep commit failure"),
    ):
        await agent_run_tasks.sweep_stale_agent_runs(
            ctx=_ctx(
                cast(
                    async_sessionmaker[AsyncSession],
                    failing_session_factory,
                )
            )
        )

    assert commit_attempted is True
    assert calls == []
    assert not [
        entry
        for entry in logs
        if entry.get("event")
        in {
            "agent_runs_stale_swept",
            "agent_user_daily_quota_stale_reservations_retained",
        }
    ]

    async with session_factory() as verification:
        persisted = await verification.get(AgentRun, stale_run.id)
    assert persisted is not None
    assert (persisted.status, persisted.error_code) == ("queued", None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failing_sink",
    ["total_log", "quota_log", "queued_metric", "running_metric"],
)
async def test_sweep_task_telemetry_sink_failure_keeps_committed_sweep_and_other_sinks(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    failing_sink: str,
) -> None:
    now = datetime.now(UTC)
    usage_date = date(2026, 7, 20)
    async with session_factory() as setup_session:
        _queued_thread, _queued_message, queued = await _create_thread_message_run(
            setup_session,
            question="queued stale telemetry isolation",
            created_at=now - timedelta(minutes=21),
            quota_usage_date=usage_date,
        )
        _running_thread, _running_message, running = await _create_thread_message_run(
            setup_session,
            question="running stale telemetry isolation",
            status="running",
            created_at=now - timedelta(minutes=30),
            started_at=now - timedelta(minutes=21),
            quota_usage_date=usage_date,
        )
    attempts: list[str] = []

    def record_total_log(event: str, **_kwargs: object) -> None:
        if event == "agent_runs_stale_swept":
            attempts.append("total_log")
            if failing_sink == "total_log":
                raise RuntimeError("total stale log sink unavailable")

    def record_quota_log(event: str, **_kwargs: object) -> None:
        if event == "agent_user_daily_quota_stale_reservations_retained":
            attempts.append("quota_log")
            if failing_sink == "quota_log":
                raise RuntimeError("quota stale log sink unavailable")

    def record_stale_reservation(*, previous_status: str, count: int = 1) -> None:
        assert count == 1
        attempts.append(f"{previous_status}_metric")
        if failing_sink == f"{previous_status}_metric":
            raise RuntimeError("quota stale metric sink unavailable")

    monkeypatch.setattr(agent_run_tasks.logger, "info", record_total_log)
    monkeypatch.setattr(daily_quota_observability.logger, "warning", record_quota_log)
    monkeypatch.setattr(
        daily_quota_observability,
        "record_daily_quota_stale_reservation",
        record_stale_reservation,
    )

    await agent_run_tasks.sweep_stale_agent_runs(ctx=_ctx(session_factory))

    assert set(attempts) == {
        "total_log",
        "quota_log",
        "queued_metric",
        "running_metric",
    }
    async with session_factory() as verification:
        persisted = [
            await verification.get(AgentRun, run_id)
            for run_id in (queued.id, running.id)
        ]
    assert [run.status if run is not None else None for run in persisted] == [
        "failed",
        "failed",
    ]


@pytest.mark.asyncio
async def test_ten_quota_queued_stale_runs_keep_counter_and_aggregate_all(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    usage_date = date(2026, 7, 20)
    async with session_factory() as setup_session:
        setup_session.add(
            AgentUserDailyQuota(
                user_id=UUID(TEST_USER_ID),
                usage_date=usage_date,
                used_count=10,
            )
        )
        await setup_session.commit()
        runs = [
            (
                await _create_thread_message_run(
                    setup_session,
                    question=f"stale quota question {index}",
                    created_at=now - timedelta(minutes=21),
                    quota_usage_date=usage_date,
                )
            )[2]
            for index in range(10)
        ]

    async with session_factory() as sweep_session:
        async with sweep_session.begin():
            result = await AgentRunRepository(sweep_session).sweep_stale_runs(now=now)

    assert (
        result.total_count,
        result.quota_queued_count,
        result.quota_running_count,
    ) == (10, 10, 0)
    async with session_factory() as verification:
        counter = await verification.scalar(
            select(AgentUserDailyQuota.used_count).where(
                AgentUserDailyQuota.user_id == UUID(TEST_USER_ID),
                AgentUserDailyQuota.usage_date == usage_date,
            )
        )
        persisted_statuses = (
            (
                await verification.execute(
                    select(AgentRun.status)
                    .where(AgentRun.id.in_([run.id for run in runs]))
                    .order_by(AgentRun.id)
                )
            )
            .scalars()
            .all()
        )
    assert counter == 10
    assert persisted_statuses == ["failed"] * 10


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
