"""Agent run worker and conversation/run repository tests."""

from __future__ import annotations

import asyncio
import logging
import traceback
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
from taskiq import InMemoryBroker
from taskiq.message import TaskiqMessage
from taskiq.receiver import Receiver

import app.agent.composition as composition
import app.queue.tasks.agent_run as agent_run_tasks
from app.agent.answering.direct_answer.failure import DirectAnswerError
from app.agent.contract import (
    AnswerGenerationStopped,
    AnswerPlanSummary,
    AnswerQuestionResult,
    ExternalUrlSource,
    InternalArticleSource,
)
from app.agent.live_updates.reporters import (
    AgentRunLiveActivityReporter,
    AgentRunLiveStageReporter,
)
from app.agent.live_updates.stream import (
    AgentRunLiveStreamAnswerDeltaEvent,
    AgentRunLiveStreamAnswerResetEvent,
    AgentRunLiveStreamStageEvent,
    AgentRunLiveStreamTerminalEvent,
)
from app.agent.planning.failure import PlanningError
from app.agent.research_handoff import (
    ResearchHandoff,
    ResearchRunRecord,
    ResearchTaskRecord,
)
from app.agent.running import (
    AnsweringPhases,
    RunIdentity,
    RunInput,
    RunResult,
)
from app.agent.runs.contracts import CompleteRunOutcome, RunTransitionLostError
from app.agent.runs.daily_quota import observability as daily_quota_observability
from app.agent.runs.repository import AgentRunRepository
from app.agent.runs.result_mapper import (
    build_assistant_message_for_result,
    build_source_rows_for_message,
)
from app.agent.runs.types import AgentRunStatus
from app.agent.runtime.contract import AgentResponseDefect, AgentResponseInvalidError
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
from app.queue.tasks.agent_run import AgentRunTaskBoundaryError
from app.shared.security.safe_url import SafeUrl
from tests.agent.runs._start_run_outcomes import (
    assert_idempotent_skip,
    started_attempt_epoch,
)
from tests.conftest import TEST_ADMIN_ID, TEST_USER_ID
from tests.logfire._metric_helpers import collected_metrics

SENSITIVE_TASK_BOUNDARY_MARKERS = (
    TEST_USER_ID,
    "SECRET_SQL_MARKER",
    "SECRET_QUESTION_MARKER",
    "SECRET_ANSWER_MARKER",
    "SECRET_PROVIDER_RAW_MARKER",
    "parameters:",
)
SENSITIVE_TASK_BOUNDARY_ERROR = (
    "asyncpg failure SECRET_SQL_MARKER: UPDATE agent_runs "
    f"parameters: ('{TEST_USER_ID}', 'SECRET_QUESTION_MARKER', "
    "'SECRET_ANSWER_MARKER', 'SECRET_PROVIDER_RAW_MARKER')"
)


class SensitivePersistenceFailure(RuntimeError):
    def __init__(self) -> None:
        super().__init__(SENSITIVE_TASK_BOUNDARY_ERROR)
        self.params = {
            "user_id": TEST_USER_ID,
            "question": "SECRET_QUESTION_MARKER",
            "answer": "SECRET_ANSWER_MARKER",
            "provider_raw": "SECRET_PROVIDER_RAW_MARKER",
        }


def _assert_safe_task_boundary_error(
    error: BaseException,
    *,
    expected_message: str,
) -> None:
    rendered_traceback = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )

    assert error.__class__ is AgentRunTaskBoundaryError
    assert str(error) == expected_message
    assert error.args == (expected_message,)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert error.__suppress_context__ is True
    assert not hasattr(error, "params")
    assert all(marker not in str(error) for marker in SENSITIVE_TASK_BOUNDARY_MARKERS)
    assert all(
        marker not in repr(vars(error)) for marker in SENSITIVE_TASK_BOUNDARY_MARKERS
    )
    assert all(
        marker not in rendered_traceback for marker in SENSITIVE_TASK_BOUNDARY_MARKERS
    )
    assert all(
        marker not in repr(error.__cause__)
        for marker in SENSITIVE_TASK_BOUNDARY_MARKERS
    )


def _assert_sensitive_task_context_not_logged(logs: object) -> None:
    serialized_logs = repr(logs)
    assert all(
        marker not in serialized_logs for marker in SENSITIVE_TASK_BOUNDARY_MARKERS
    )


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
        self.calls: list[object] = []

    async def answer(self) -> AnswerQuestionResult:
        self.calls.append(None)
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
    identity: RunIdentity


class FakeAnsweringRunner:
    def __init__(
        self,
        *,
        exc: BaseException | None = None,
        research_handoff: ResearchHandoff | None = None,
    ) -> None:
        self.exc = exc
        self.research_handoff = research_handoff
        self.execution: object | None = None
        self.calls: list[FakeAnsweringRunnerCall] = []

    async def run(
        self,
        input: RunInput,
        *,
        identity: RunIdentity,
    ) -> RunResult:
        self.calls.append(
            FakeAnsweringRunnerCall(
                input=input,
                identity=identity,
            )
        )
        if self.exc is not None:
            raise self.exc
        assert self.execution is not None
        final_output = await cast(Any, self.execution).answer()
        return RunResult(
            final_output=final_output,
            research_handoff=self.research_handoff,
        )


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

    async def answer(self) -> AnswerQuestionResult:
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

    async def answer(self) -> AnswerQuestionResult:
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


class ForbiddenConstruction:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("start skip後にlive dependencyを生成してはいけません")


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
    FakeLiveStreamPublisher.instances = []
    monkeypatch.setattr(FakeLiveStreamPublisher, "publish_outcomes", [])
    _patch_worker_execution(monkeypatch, builder)
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveStreamPublisher",
        stream_publisher,
    )


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


def test_composition_injects_same_live_controls_into_both_answer_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.agent.answering.direct_answer.service as direct_service_module
    import app.agent.answering.evidence_answer.service as evidence_service_module
    import app.agent.evidence_collection.internal_search.ai.gemini as embedder_module
    import app.agent.planning.service as planning_service_module
    from app.agent.answering.direct_answer.agent import DIRECT_ANSWER_AGENT
    from app.agent.answering.evidence_answer.agent import EVIDENCE_ANSWER_AGENT
    from app.agent.evidence_collection.internal_search import (
        article_repository as article_repository_module,
    )
    from app.agent.evidence_collection.internal_search import (
        service as internal_search_module,
    )

    captured: dict[str, dict[str, object]] = {}
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
    monkeypatch.setattr(direct_service_module, "DirectAnswerService", capture_direct)
    monkeypatch.setattr(
        evidence_service_module, "EvidenceAnswerService", capture_evidence
    )
    monkeypatch.setattr(embedder_module, "GeminiQueryEmbedder", lambda: object())
    monkeypatch.setattr(
        article_repository_module,
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
    assert phases.collector.internal_search is internal_search
    assert phases.direct_answerer is not None
    assert phases.evidence_answerer is not None


async def _create_thread_message_run(
    session: AsyncSession,
    *,
    status: str = "queued",
    question: str = "worker question",
    history: list[tuple[str, str] | tuple[str, str, list[object]]] | None = None,
    created_at: datetime | None = None,
    started_at: datetime | None = None,
    attempt_epoch: int | None = None,
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
    effective_created_at = created_at or datetime.now(UTC)
    run = AgentRun(
        thread_id=thread.id,
        user_message_id=message.id,
        status=status,
        created_at=effective_created_at,
        deadline_at=effective_created_at + timedelta(seconds=60),
        started_at=started_at,
        error_code=error_code,
        quota_usage_date=quota_usage_date,
    )
    if attempt_epoch is not None:
        run.attempt_epoch = attempt_epoch
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
        research_handoff: dict[str, Any] | None = None,
    ) -> CompleteRunOutcome:
        persisted_results.append(result)
        completed_epochs.append(expected_attempt_epoch)
        return await original_complete(
            repository,
            run_id=run_id,
            result=result,
            expected_attempt_epoch=expected_attempt_epoch,
            research_handoff=research_handoff,
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


def _capture_complete_run_handoffs(
    monkeypatch: pytest.MonkeyPatch,
) -> list[dict[str, Any] | None]:
    """complete_run へ渡る handoff payload を記録しつつ、本物へ委譲する。"""
    captured: list[dict[str, Any] | None] = []
    original_complete = AgentRunRepository.complete_run

    async def capture(
        repository: AgentRunRepository,
        *,
        run_id: UUID,
        result: AnswerQuestionResult,
        expected_attempt_epoch: int,
        research_handoff: dict[str, Any] | None = None,
    ) -> CompleteRunOutcome:
        captured.append(research_handoff)
        return await original_complete(
            repository,
            run_id=run_id,
            result=result,
            expected_attempt_epoch=expected_attempt_epoch,
            research_handoff=research_handoff,
        )

    monkeypatch.setattr(AgentRunRepository, "complete_run", capture)
    return captured


@pytest.mark.asyncio
async def test_run_agent_answer_passes_the_recalled_handoff_to_planning_request(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """threadに保存済みのhandoffがRunInput.research_handoffとして次のrunへ渡る。"""
    handoff = _handoff()
    async with session_factory() as session:
        thread, _current_message, current_run = await _create_thread_message_run(
            session,
            question="new question",
        )
        thread.research_handoff = handoff.model_dump(mode="json")
        await session.commit()

    answering_runner = _patch_worker_execution(
        monkeypatch,
        lambda **_kwargs: FakeAgent(_direct_result()),
    )

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=current_run.id),
        ctx=_ctx(session_factory),
    )

    assert len(answering_runner.calls) == 1
    assert answering_runner.calls[0].input.research_handoff == handoff


@pytest.mark.asyncio
async def test_run_agent_answer_continues_without_a_handoff_when_read_fails(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """読出し失敗はresearch_handoff=Noneに落として回答workflowを継続する。"""
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)

    async def failing_read(
        self: AgentThreadRepository, **_kwargs: object
    ) -> dict[str, Any] | None:
        raise RuntimeError("research handoff read boundary failure")

    monkeypatch.setattr(
        AgentThreadRepository,
        "read_research_handoff_for_user",
        failing_read,
    )
    answering_runner = _patch_worker_execution(
        monkeypatch,
        lambda **_kwargs: FakeAgent(_direct_result()),
    )

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )

    assert len(answering_runner.calls) == 1
    assert answering_runner.calls[0].input.research_handoff is None
    async with session_factory() as session:
        completed = await session.get(AgentRun, run.id)
        assert completed is not None
        assert completed.status == "completed"


@pytest.mark.asyncio
async def test_run_agent_answer_forwards_the_serialized_handoff_to_complete_run(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RunResult.research_handoffはmodel_dump(mode="json")のdictへ変換されてから

    complete_runへ渡る(agent_threads.research_handoffの正本はJSON snake_caseで
    あり、pydantic modelそのものはDBへ渡さない)。
    """
    async with session_factory() as session:
        thread, _message, run = await _create_thread_message_run(session)
        thread_id = thread.id
    fake_agent = FakeAgent(_direct_result())
    handoff = _handoff()
    expected_handoff_json = {
        "schema_version": 1,
        "updated_at": "2026-08-03T09:00:00Z",
        "runs": [
            {
                "as_of": "2026-08-03T09:00:00Z",
                "tasks": [
                    {
                        "research_goal": "調査目標",
                        "executed_queries": ["q-a"],
                    }
                ],
            }
        ],
        "collected_overview": "供給網の記事が集まった",
        "unresolved_points": "在庫水準は確認できていない",
        "next_search_guidance": "決算資料をあたる",
    }
    answering_runner = _patch_worker_execution(
        monkeypatch,
        lambda **_kwargs: fake_agent,
        answering_runner=FakeAnsweringRunner(research_handoff=handoff),
    )
    captured_handoffs = _capture_complete_run_handoffs(monkeypatch)

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )

    assert len(answering_runner.calls) == 1
    assert captured_handoffs == [expected_handoff_json]
    async with session_factory() as session:
        completed = await session.get(AgentRun, run.id)
        persisted_thread = await session.get(AgentThread, thread_id)
        assert completed is not None and persisted_thread is not None
        assert completed.status == "completed"
        assert persisted_thread.research_handoff == expected_handoff_json


@pytest.mark.asyncio
async def test_run_agent_answer_forwards_none_when_run_result_has_no_handoff(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """記録を追加しなかったRunはNoneのままcomplete_runへ渡る。"""
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    fake_agent = FakeAgent(_direct_result())
    _patch_worker_execution(
        monkeypatch,
        lambda **_kwargs: fake_agent,
        answering_runner=FakeAnsweringRunner(research_handoff=None),
    )
    captured_handoffs = _capture_complete_run_handoffs(monkeypatch)

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )

    assert captured_handoffs == [None]


@pytest.mark.asyncio
async def test_run_agent_answer_treats_handoff_serialization_failure_as_none(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """model_dump失敗はcomplete_run呼び出し前に完結し、Noneとして渡る。

    taskはfailedにせず正常に完了する。
    """
    async with session_factory() as session:
        thread, _message, run = await _create_thread_message_run(session)
        thread_id = thread.id
    fake_agent = FakeAgent(_direct_result())

    def _raise_model_dump_failure(
        self: ResearchHandoff, *_args: object, **_kwargs: object
    ) -> None:
        del self
        raise RuntimeError("model_dump boom")

    monkeypatch.setattr(ResearchHandoff, "model_dump", _raise_model_dump_failure)
    _patch_worker_execution(
        monkeypatch,
        lambda **_kwargs: fake_agent,
        answering_runner=FakeAnsweringRunner(research_handoff=_handoff()),
    )
    captured_handoffs = _capture_complete_run_handoffs(monkeypatch)

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )

    assert captured_handoffs == [None]
    async with session_factory() as session:
        completed = await session.get(AgentRun, run.id)
        persisted_thread = await session.get(AgentThread, thread_id)
        assert completed is not None and persisted_thread is not None
        assert completed.status == "completed"
        assert persisted_thread.research_handoff is None


@pytest.mark.asyncio
async def test_completed_run_persists_only_fixed_time_filter_missing_aspect(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_missing = "指定された公開期間を外部検索へ適用できませんでした"
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    result = AnswerQuestionResult(
        status="insufficient",
        answer="指定期間の外部根拠は取得できませんでした。",
        sources=[],
        missing_aspects=[fixed_missing],
        plan_summary=_plan_summary("search"),
    )
    _patch_worker_execution(monkeypatch, lambda **_kwargs: FakeAgent(result))

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )

    async with session_factory() as session:
        completed = await session.get(AgentRun, run.id)
        assert completed is not None
        assert completed.assistant_message_id is not None
        assistant = await session.get(AgentMessage, completed.assistant_message_id)
        assert assistant is not None

    assert (
        completed.status,
        assistant.role,
        assistant.missing_aspects,
    ) == ("completed", "assistant", [fixed_missing])


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
    answering_runner = FakeAnsweringRunner()
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
    assert answering_runner.calls[0].input.question == follow_up_question


@pytest.mark.asyncio
async def test_run_agent_answer_completion_preserves_last_progress_stage(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    fake_agent = FakeAgent(_direct_result(), stage="answering")
    FakeLiveStreamPublisher.instances = []

    def build_agent(**kwargs: object) -> FakeAgent:
        fake_agent.progress = kwargs["progress"]
        return fake_agent

    _patch_worker_execution(monkeypatch, build_agent)
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
    stream = FakeLiveStreamPublisher.instances[0]
    stages = [
        event
        for event in stream.published
        if isinstance(event, AgentRunLiveStreamStageEvent)
    ]
    assert [event.stage for event in stages] == ["answering"]


@pytest.mark.asyncio
async def test_run_agent_answer_injects_activity_reporter(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    fake_agent = FakeAgent(_direct_result())
    captured_kwargs: dict[str, object] = {}

    def build_agent(**kwargs: object) -> FakeAgent:
        captured_kwargs.update(kwargs)
        return fake_agent

    redis = object()
    _patch_worker_execution(monkeypatch, build_agent)
    monkeypatch.setattr(agent_run_tasks, "get_redis", lambda: redis)
    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )

    assert isinstance(captured_kwargs["events"], AgentRunLiveActivityReporter)


@pytest.mark.asyncio
async def test_run_agent_answer_starts_stream_attempt_only_after_start(
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

    def build_agent(**kwargs: object) -> FakeAgent:
        captured_kwargs.update(kwargs)
        return fake_agent

    _patch_worker_execution(monkeypatch, build_agent)
    monkeypatch.setattr(agent_run_tasks, "get_redis", lambda: redis)
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

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )

    assert len(CapturingDeltaReporter.instances) == 1
    assert len(CapturingExecutionProbe.instances) == 1
    stream = FakeLiveStreamPublisher.instances[0]
    delta_reporter = CapturingDeltaReporter.instances[0]
    probe = CapturingExecutionProbe.instances[0]
    assert delta_reporter.publisher is stream
    assert delta_reporter.run_id == run.id
    assert delta_reporter.attempt_epoch == 1
    assert probe.session_factory is session_factory
    assert probe.run_id == run.id
    assert probe.attempt_epoch == 1
    assert isinstance(captured_kwargs["progress"], AgentRunLiveStageReporter)
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
            "start skip後にexecution dependencyをbuildしてはいけません",
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
async def test_run_agent_answer_passes_answering_runner_identity_and_history(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    question = "それの株価への影響は？"
    async with session_factory() as session:
        thread, _message, run = await _create_thread_message_run(
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
    answering_runner = FakeAnsweringRunner()
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
        "make_external_async_client",
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
    assert answering_runner_call.identity == RunIdentity(
        user_id=UUID(TEST_USER_ID),
        run_id=run.id,
        thread_id=thread.id,
        as_of=datetime(2026, 7, 16, 9, 30, tzinfo=UTC),
    )
    assert answering_runner_call.identity.as_of.utcoffset() == timedelta(0)
    assert len(runner_execution.calls) == 1
    assert len(runner_builder_calls) == 1
    runner_kwargs = runner_builder_calls[0]
    assert runner_kwargs["session_factory"] is session_factory
    assert isinstance(runner_kwargs["events"], AgentRunLiveActivityReporter)
    assert runner_kwargs["progress"] is not None
    assert runner_kwargs["delta_reporter"] is not None
    assert runner_kwargs["continuation"] is not None
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
    ) -> CompleteRunOutcome:
        assert result == fake_agent.result
        complete_calls.append((run_id, expected_attempt_epoch))
        return CompleteRunOutcome.TRANSITION_LOST

    async def observe_mark_failed(
        _repository: AgentRunRepository,
        run_id: UUID,
        *,
        error_code: agent_run_tasks.AgentRunErrorCode,
        expected_attempt_epoch: int,
    ) -> CompleteRunOutcome:
        assert error_code == agent_run_tasks.AgentRunErrorCode.INTERNAL_ERROR
        mark_failed_calls.append((run_id, expected_attempt_epoch))
        return CompleteRunOutcome.TRANSITION_LOST

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

        async def answer(self) -> AnswerQuestionResult:
            assert self.continuation is not None
            assert await self.continuation.should_continue() is True  # type: ignore[attr-defined]
            async with session_factory() as restart_session:
                async with restart_session.begin():
                    attempt_epoch = started_attempt_epoch(
                        await AgentRunRepository(restart_session).start_run(run.id)
                    )
            assert attempt_epoch == 2
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
    ) -> CompleteRunOutcome:
        assert result == fake_agent.result
        complete_calls.append((run_id, expected_attempt_epoch))
        return CompleteRunOutcome.TRANSITION_LOST

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
        research_handoff: dict[str, Any] | None = None,
    ) -> bool:
        order.append("complete_start")
        assert expected_attempt_epoch == 1
        return await original_complete(
            repository,
            run_id=run_id,
            result=result,
            expected_attempt_epoch=expected_attempt_epoch,
            research_handoff=research_handoff,
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
        research_handoff: dict[str, Any] | None = None,
    ) -> CompleteRunOutcome:
        assert (run_id, result, expected_attempt_epoch) == (
            run.id,
            fake_agent.result,
            1,
        )
        if completion_outcome == "lost":
            raise RunTransitionLostError
        return CompleteRunOutcome.TRANSITION_LOST

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
        research_handoff: dict[str, Any] | None = None,
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
        research_handoff: dict[str, Any] | None = None,
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
        research_handoff: dict[str, Any] | None = None,
    ) -> CompleteRunOutcome:
        assert (run_id, result, expected_attempt_epoch) == (
            run.id,
            fake_agent.result,
            1,
        )
        return CompleteRunOutcome.TRANSITION_LOST

    _patch_worker_execution(monkeypatch, lambda **_kwargs: fake_agent)
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
    fake_agent = FakeAgent(_direct_result(), stage="answering")
    FakeLiveStreamPublisher.instances = []
    monkeypatch.setattr(FakeLiveStreamPublisher, "raise_on_publish", True)

    def build_agent(**kwargs: object) -> FakeAgent:
        fake_agent.progress = kwargs["progress"]
        return fake_agent

    _patch_worker_execution(monkeypatch, build_agent)
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
    answering_runner = FakeAnsweringRunner()
    _patch_worker_execution(
        monkeypatch,
        lambda **_kwargs: fake_agent,
        answering_runner=answering_runner,
    )
    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )

    assert len(answering_runner.calls) == 1
    assert answering_runner.calls[0].input == RunInput(question=question, history=())
    assert len(fake_agent.calls) == 1


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
    _patch_worker_execution(
        monkeypatch,
        lambda **_kwargs: fake_agent,
        answering_runner=answering_runner,
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
            started_at=datetime.now(UTC),
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
            started_at=datetime.now(UTC),
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


def _direct_answer_error_with_private_cause() -> DirectAnswerError:
    error = DirectAnswerError(code="direct_answer_blank_response")
    error.__cause__ = AIProviderError("SHOULD_NOT_LEAK")
    return error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "generation_error",
    [
        AIProviderConfigurationError(),
        AIProviderError("SHOULD_NOT_LEAK"),
        _direct_answer_error_with_private_cause(),
        AgentResponseInvalidError(AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH),
        PlanningError(code="ai_error_network"),
    ],
    ids=(
        "configuration",
        "provider",
        "direct-answer",
        "invalid-agent-output",
        "planning",
    ),
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

    with capture_logs() as logs:
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
    assert "SHOULD_NOT_LEAK" not in repr(logs)


@pytest.mark.asyncio
async def test_run_agent_answer_generation_error_preserves_death_progress_stage(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    fake_agent = FakeAgent(
        exc=AIProviderError("SHOULD_NOT_LEAK"), stage="evidence_collection"
    )

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
    assert_idempotent_skip(skipped)
    async with session_factory() as session:
        restarted = await session.get(AgentRun, running.id)
        terminal = await session.get(AgentRun, failed.id)
        assert restarted is not None
        assert terminal is not None
        assert (
            restarted.status,
            restarted.started_at,
            restarted.attempt_epoch,
        ) == ("running", now, 2)
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

    assert_idempotent_skip(skip_result)


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

    assert_idempotent_skip(skip_result)
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
            session, created_at=now - timedelta(minutes=4)
        )
        _t5, _m5, terminal = await _create_thread_message_run(
            session,
            status="failed",
            created_at=now - timedelta(minutes=30),
            error_code="internal_error",
        )
        result = await AgentRunRepository(session).sweep_stale_runs(now=now)
        await session.commit()
        assert result.queued_terminal_count == 2
        assert result.queued_quota_released_count == 0
        assert result.queued_quota_not_eligible_count == 1
        assert result.queued_quota_inconsistent_count == 1
        running_attempts = [
            (item.run_id, item.attempt_epoch) for item in result.running_terminal_runs
        ]
        assert running_attempts == [(old_running.id, 7)]
        assert result.running_quota_reservation_count == 1
        assert result.running_without_started_at_count == 0

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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sweep_task_observes_queued_release_and_running_reservation_after_commit(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    usage_date = date(2026, 7, 20)
    async with session_factory() as session:
        session.add(
            AgentUserDailyQuota(
                user_id=UUID(TEST_USER_ID),
                usage_date=usage_date,
                used_count=1,
            )
        )
        _t1, _m1, old_queued = await _create_thread_message_run(
            session,
            question="sensitive queued question",
            created_at=now - timedelta(minutes=6),
            quota_usage_date=usage_date,
        )
        _t2, _m2, old_running = await _create_thread_message_run(
            session,
            question="sensitive running question",
            status="running",
            created_at=now - timedelta(minutes=30),
            started_at=now - timedelta(minutes=6),
            attempt_epoch=2,
            quota_usage_date=usage_date,
        )
        _t3, _m3, old_legacy = await _create_thread_message_run(
            session,
            question="sensitive legacy question",
            created_at=now - timedelta(minutes=6),
        )

    release_metrics: list[dict[str, object]] = []
    reservations: list[dict[str, int]] = []

    def record_release(**kwargs: object) -> None:
        release_metrics.append(kwargs)

    def observe_stale_reservations(*, queued_count: int, running_count: int) -> None:
        reservations.append(
            {"queued_count": queued_count, "running_count": running_count}
        )

    monkeypatch.setattr(
        daily_quota_observability,
        "record_daily_quota_release",
        record_release,
    )
    monkeypatch.setattr(
        daily_quota_observability,
        "observe_stale_reservations",
        observe_stale_reservations,
    )
    monkeypatch.setattr(agent_run_tasks, "get_redis", object)
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveStreamPublisher",
        FakeLiveStreamPublisher,
    )

    with capture_logs() as logs:
        await agent_run_tasks.sweep_stale_agent_runs(ctx=_ctx(session_factory))

    assert [
        entry for entry in logs if entry.get("event") == "agent_runs_queued_stale_swept"
    ] == [
        {
            "event": "agent_runs_queued_stale_swept",
            "log_level": "info",
            "run_count": 2,
            "quota_released_count": 1,
            "quota_not_eligible_count": 1,
            "quota_inconsistent_count": 0,
        }
    ]
    assert release_metrics == [
        {"result": "released", "count": 1},
        {"result": "not_eligible", "count": 1},
    ]
    assert reservations == [{"queued_count": 0, "running_count": 1}]
    assert "sensitive" not in str(logs)

    async with session_factory() as session:
        counter = await session.scalar(
            select(AgentUserDailyQuota.used_count).where(
                AgentUserDailyQuota.user_id == UUID(TEST_USER_ID),
                AgentUserDailyQuota.usage_date == usage_date,
            )
        )
        statuses = [
            await session.get(AgentRun, run_id)
            for run_id in (old_queued.id, old_running.id, old_legacy.id)
        ]
    assert counter == 0
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
        raise SensitivePersistenceFailure

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
        pytest.raises(Exception) as exc_info,
    ):
        await agent_run_tasks.sweep_stale_agent_runs(
            ctx=_ctx(
                cast(
                    async_sessionmaker[AsyncSession],
                    failing_session_factory,
                )
            )
        )

    _assert_safe_task_boundary_error(
        exc_info.value,
        expected_message="agent run stale sweep failed",
    )
    _assert_sensitive_task_context_not_logged(logs)
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
async def test_ten_quota_queued_stale_runs_release_counter_with_one_batch_update(
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

    assert result.queued_terminal_count == 10
    assert result.queued_quota_released_count == 10
    assert result.queued_quota_not_eligible_count == 0
    assert result.queued_quota_inconsistent_count == 0
    assert result.running_terminal_runs == ()
    assert result.running_quota_reservation_count == 0
    assert result.running_without_started_at_count == 0
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
    assert counter == 0
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
        plan_summary=_plan_summary("search"),
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


@dataclass
class _ControlledMonotonicClock:
    now: float

    def __call__(self) -> float:
        return self.now


class _ControlledApplicationDeadline:
    def __init__(
        self,
        *,
        deadline: float,
        clock: _ControlledMonotonicClock,
        expire_on_cancel: bool,
    ) -> None:
        self.deadline = deadline
        self._clock = clock
        self._expire_on_cancel = expire_on_cancel
        self._cancel_converted = False
        self.active = False

    async def __aenter__(self) -> _ControlledApplicationDeadline:
        self.active = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: object | None,
    ) -> bool:
        self.active = False
        if self._expire_on_cancel and exc_type is asyncio.CancelledError:
            self._cancel_converted = True
            raise TimeoutError
        return False

    def expired(self) -> bool:
        return self._cancel_converted


def _install_application_deadline_boundary(
    monkeypatch: pytest.MonkeyPatch,
    clock: _ControlledMonotonicClock,
    *,
    expire_on_cancel: bool = False,
) -> list[_ControlledApplicationDeadline]:
    task_asyncio = getattr(agent_run_tasks, "asyncio", None)
    assert task_asyncio is not None, "task must import asyncio for its deadline"
    task_time = getattr(agent_run_tasks, "time", None)
    assert task_time is not None, "task must use a monotonic application clock"
    timeout_at = getattr(task_asyncio, "timeout_at", None)
    assert callable(timeout_at), "task must own its deadline with asyncio.timeout_at"
    scopes: list[_ControlledApplicationDeadline] = []

    def controlled_timeout_at(deadline: float) -> _ControlledApplicationDeadline:
        scope = _ControlledApplicationDeadline(
            deadline=deadline,
            clock=clock,
            expire_on_cancel=expire_on_cancel,
        )
        scopes.append(scope)
        return scope

    monkeypatch.setattr(task_time, "monotonic", clock)
    monkeypatch.setattr(task_asyncio, "timeout_at", controlled_timeout_at)
    return scopes


def test_run_agent_answer_declares_fixed_application_and_taskiq_deadlines() -> None:
    """application deadline が taskiq timeout より先に切れることで、

    graceful な失敗処理 (terminal 化・cleanup) が taskiq の強制 kill より
    先に走る。retry は attempt_epoch で run 側が管理するため taskiq の
    自動 retry は常に無効。
    """
    task = agent_run_tasks.run_agent_answer

    assert task.task_name == "run_agent_answer"
    assert task.labels == {
        "timeout": agent_run_tasks.RESEARCH_TASKIQ_TIMEOUT_SECONDS,
        "max_retries": 0,
        "retry_on_error": False,
    }
    assert (
        agent_run_tasks.RESEARCH_APPLICATION_TIMEOUT_SECONDS
        < agent_run_tasks.RESEARCH_TASKIQ_TIMEOUT_SECONDS
    )


@pytest.mark.asyncio
async def test_taskiq_receiver_result_and_log_expose_only_safe_start_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def fail_start(*_args: object, **_kwargs: object) -> object:
        raise SensitivePersistenceFailure

    monkeypatch.setattr(agent_run_tasks, "_start_run", fail_start)
    broker = InMemoryBroker()
    broker.state.session_factory = object()
    receiver = Receiver(
        broker,
        max_async_tasks=1,
        run_startup=False,
    )
    message = TaskiqMessage(
        task_id="safe-boundary-test",
        task_name="run_agent_answer",
        labels={},
        args=[],
        kwargs={
            "trigger": AgentRunTrigger(
                run_id=UUID("00000000-0000-4000-a000-000000000010")
            )
        },
    )

    with caplog.at_level(logging.ERROR, logger="taskiq.receiver.receiver"):
        result = await receiver.run_task(
            agent_run_tasks.run_agent_answer.original_func,
            message,
        )

    assert result.is_err is True
    assert result.error is not None
    _assert_safe_task_boundary_error(
        result.error,
        expected_message="agent run start failed",
    )
    receiver_output = f"{result!r}\n{caplog.text}"
    assert all(
        marker not in receiver_output for marker in SENSITIVE_TASK_BOUNDARY_MARKERS
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_start_commit_failure_raises_sanitized_task_boundary_error(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usage_date = date(2026, 7, 22)
    async with session_factory() as setup_session:
        setup_session.add(
            AgentUserDailyQuota(
                user_id=UUID(TEST_USER_ID),
                usage_date=usage_date,
                used_count=1,
            )
        )
        _thread, _message, run = await _create_thread_message_run(
            setup_session,
            question="SECRET_QUESTION_MARKER",
            created_at=datetime.now(UTC) - timedelta(minutes=4),
            quota_usage_date=usage_date,
        )

    failing_session = session_factory()

    def fail_start_commit(_session: object) -> None:
        raise SensitivePersistenceFailure

    sa_event.listen(
        failing_session.sync_session,
        "before_commit",
        fail_start_commit,
        once=True,
    )

    def failing_session_factory() -> AsyncSession:
        return failing_session

    FakeLiveStreamPublisher.instances = []
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveStreamPublisher",
        FakeLiveStreamPublisher,
    )

    try:
        with capture_logs() as logs, pytest.raises(Exception) as exc_info:
            await agent_run_tasks.run_agent_answer(
                trigger=AgentRunTrigger(run_id=run.id),
                ctx=_ctx(
                    cast(
                        async_sessionmaker[AsyncSession],
                        failing_session_factory,
                    )
                ),
            )
    finally:
        await failing_session.close()

    _assert_safe_task_boundary_error(
        exc_info.value,
        expected_message="agent run start failed",
    )
    _assert_sensitive_task_context_not_logged(logs)
    assert FakeLiveStreamPublisher.instances == []
    async with session_factory() as verification:
        persisted = await verification.get(AgentRun, run.id)
        quota_count = await verification.scalar(
            select(AgentUserDailyQuota.used_count).where(
                AgentUserDailyQuota.user_id == UUID(TEST_USER_ID),
                AgentUserDailyQuota.usage_date == usage_date,
            )
        )
    assert persisted is not None
    assert (persisted.status, persisted.error_code, quota_count) == (
        "queued",
        None,
        1,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_application_deadline_scope_covers_start_live_history_runner_and_result(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application_timeout = agent_run_tasks.RESEARCH_APPLICATION_TIMEOUT_SECONDS
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    clock = _ControlledMonotonicClock(now=100.0)
    scopes = _install_application_deadline_boundary(monkeypatch, clock)
    seen_steps: list[str] = []
    original_start = agent_run_tasks._start_run
    original_history = agent_run_tasks._read_history

    async def start_within_deadline(*args: object, **kwargs: object) -> object:
        assert len(scopes) == 1 and scopes[0].active
        seen_steps.append("start")
        return await original_start(*args, **kwargs)  # type: ignore[arg-type]

    async def history_within_deadline(*args: object, **kwargs: object) -> object:
        assert scopes[0].active
        seen_steps.append("history")
        return await original_history(*args, **kwargs)  # type: ignore[arg-type]

    class ScopeCheckingStream(FakeLiveStreamPublisher):
        async def begin_attempt(self) -> str | None:
            assert scopes[0].active
            seen_steps.append("begin_attempt")
            return await super().begin_attempt()

    class ScopeCheckingAgent(FakeAgent):
        async def answer(self) -> AnswerQuestionResult:
            assert scopes[0].active
            seen_steps.append("result")
            return await super().answer()

    class ScopeCheckingRunner(FakeAnsweringRunner):
        async def run(
            self,
            input: RunInput,
            *,
            identity: RunIdentity,
        ) -> RunResult:
            assert scopes[0].active
            seen_steps.append("runner")
            return await super().run(input, identity=identity)

    agent = ScopeCheckingAgent(_direct_result())
    runner = ScopeCheckingRunner()

    def build_runner(**kwargs: object) -> ScopeCheckingRunner:
        assert scopes[0].active
        seen_steps.append("build")
        runner.execution = agent
        return runner

    monkeypatch.setattr(agent_run_tasks, "_start_run", start_within_deadline)
    monkeypatch.setattr(agent_run_tasks, "_read_history", history_within_deadline)
    monkeypatch.setattr(agent_run_tasks, "get_redis", object)
    monkeypatch.setattr(
        agent_run_tasks, "AgentRunLiveStreamPublisher", ScopeCheckingStream
    )
    monkeypatch.setattr(agent_run_tasks, "build_answering_runner", build_runner)

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )

    assert [scope.deadline for scope in scopes] == [100.0 + application_timeout]
    assert seen_steps == [
        "start",
        "begin_attempt",
        "history",
        "build",
        "runner",
        "result",
    ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_start_after_application_deadline_skips_execution_and_terminalizes(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    application_timeout = agent_run_tasks.RESEARCH_APPLICATION_TIMEOUT_SECONDS
    clock = _ControlledMonotonicClock(now=100.0)
    scopes = _install_application_deadline_boundary(monkeypatch, clock)
    original_start = agent_run_tasks._start_run
    original_mark_failed = AgentRunRepository.mark_failed
    FakeLiveStreamPublisher.instances = []

    async def start_then_expire(*args: object, **kwargs: object) -> object:
        assert scopes[0].active
        result = await original_start(*args, **kwargs)  # type: ignore[arg-type]
        clock.now = 100.0 + application_timeout + 1
        assert scopes[0].expired() is False
        return result

    async def cleanup_outside_deadline(
        repository: AgentRunRepository,
        *args: object,
        **kwargs: object,
    ) -> bool:
        assert scopes[0].active is False
        return await original_mark_failed(repository, *args, **kwargs)  # type: ignore[arg-type]

    class CommitCheckingPublisher(FakeLiveStreamPublisher):
        async def publish(self, event: object) -> str | None:
            if isinstance(event, AgentRunLiveStreamTerminalEvent):
                async with session_factory() as verification:
                    persisted = await verification.get(AgentRun, run.id)
                    assert persisted is not None
                    assert (
                        persisted.status,
                        persisted.error_code,
                        persisted.attempt_epoch,
                    ) == ("failed", "generation_unavailable", 1)
            return await super().publish(event)

    def forbidden_runner(*_args: object, **_kwargs: object) -> None:
        pytest.fail("start後にdeadline超過したrunはrunnerを開始してはいけません")

    monkeypatch.setattr(agent_run_tasks, "_start_run", start_then_expire)
    monkeypatch.setattr(AgentRunRepository, "mark_failed", cleanup_outside_deadline)
    monkeypatch.setattr(agent_run_tasks, "get_redis", object)
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveStreamPublisher",
        CommitCheckingPublisher,
    )
    monkeypatch.setattr(agent_run_tasks, "build_answering_runner", forbidden_runner)
    monkeypatch.setattr(agent_run_tasks, "_read_history", forbidden_runner)

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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_application_timeout_commits_current_attempt_before_terminal_publish(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usage_date = date(2026, 7, 22)
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(
            session,
            quota_usage_date=usage_date,
        )
        session.add(
            AgentUserDailyQuota(
                user_id=UUID(TEST_USER_ID),
                usage_date=usage_date,
                used_count=1,
            )
        )
        await session.commit()
    clock = _ControlledMonotonicClock(now=100.0)
    scopes = _install_application_deadline_boundary(
        monkeypatch,
        clock,
        expire_on_cancel=True,
    )
    original_mark_failed = AgentRunRepository.mark_failed
    FakeLiveStreamPublisher.instances = []

    async def cleanup_outside_deadline(
        repository: AgentRunRepository,
        *args: object,
        **kwargs: object,
    ) -> bool:
        assert scopes[0].active is False
        return await original_mark_failed(repository, *args, **kwargs)  # type: ignore[arg-type]

    class CommitCheckingPublisher(FakeLiveStreamPublisher):
        async def publish(self, event: object) -> str | None:
            if isinstance(event, AgentRunLiveStreamTerminalEvent):
                async with session_factory() as verification:
                    persisted = await verification.get(AgentRun, run.id)
                    assert persisted is not None
                    assert (persisted.status, persisted.error_code) == (
                        "failed",
                        "generation_unavailable",
                    )
            return await super().publish(event)

    _patch_worker_execution(
        monkeypatch,
        lambda **_kwargs: FakeAgent(exc=asyncio.CancelledError()),
    )
    monkeypatch.setattr(AgentRunRepository, "mark_failed", cleanup_outside_deadline)
    monkeypatch.setattr(agent_run_tasks, "get_redis", object)
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveStreamPublisher",
        CommitCheckingPublisher,
    )

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )

    assert scopes[0].expired() is True
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
    async with session_factory() as verification:
        counter = await verification.scalar(
            select(AgentUserDailyQuota.used_count).where(
                AgentUserDailyQuota.user_id == UUID(TEST_USER_ID),
                AgentUserDailyQuota.usage_date == usage_date,
            )
        )
    assert counter == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_application_timeout_publish_failure_keeps_committed_failed_run(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    clock = _ControlledMonotonicClock(now=100.0)
    _install_application_deadline_boundary(
        monkeypatch,
        clock,
        expire_on_cancel=True,
    )
    FakeLiveStreamPublisher.instances = []
    monkeypatch.setattr(FakeLiveStreamPublisher, "raise_on_publish", True)
    _patch_worker_execution(
        monkeypatch,
        lambda **_kwargs: FakeAgent(exc=asyncio.CancelledError()),
    )
    monkeypatch.setattr(agent_run_tasks, "get_redis", object)
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveStreamPublisher",
        FakeLiveStreamPublisher,
    )

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )

    async with session_factory() as verification:
        persisted = await verification.get(AgentRun, run.id)
    assert persisted is not None
    assert (persisted.status, persisted.error_code) == (
        "failed",
        "generation_unavailable",
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_timed_out_old_attempt_cannot_terminalize_newer_attempt_or_publish(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    clock = _ControlledMonotonicClock(now=100.0)
    _install_application_deadline_boundary(
        monkeypatch,
        clock,
        expire_on_cancel=True,
    )
    FakeLiveStreamPublisher.instances = []

    class EpochAdvancingThenCancelledAgent:
        async def answer(self) -> AnswerQuestionResult:
            async with session_factory() as session:
                async with session.begin():
                    newer = started_attempt_epoch(
                        await AgentRunRepository(session).start_run(run.id)
                    )
            assert newer == 2
            raise asyncio.CancelledError

    _patch_worker_execution(
        monkeypatch,
        lambda **_kwargs: EpochAdvancingThenCancelledAgent(),
    )
    monkeypatch.setattr(agent_run_tasks, "get_redis", object)
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveStreamPublisher",
        FakeLiveStreamPublisher,
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
    assert terminal == []
    async with session_factory() as verification:
        persisted = await verification.get(AgentRun, run.id)
    assert persisted is not None
    assert (persisted.status, persisted.error_code, persisted.attempt_epoch) == (
        "running",
        None,
        2,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_application_timeout_cleanup_commit_failure_propagates_without_terminal(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as setup_session:
        _thread, _message, run = await _create_thread_message_run(setup_session)
    clock = _ControlledMonotonicClock(now=100.0)
    _install_application_deadline_boundary(
        monkeypatch,
        clock,
        expire_on_cancel=True,
    )
    FakeLiveStreamPublisher.instances = []
    start_session = session_factory()
    history_session = session_factory()
    cleanup_session = session_factory()

    def fail_cleanup_commit(_session: object) -> None:
        raise SensitivePersistenceFailure

    sa_event.listen(
        cleanup_session.sync_session,
        "before_commit",
        fail_cleanup_commit,
        once=True,
    )
    sessions = iter([start_session, history_session, cleanup_session])

    def controlled_session_factory() -> AsyncSession:
        return next(sessions)

    _patch_worker_execution(
        monkeypatch,
        lambda **_kwargs: FakeAgent(exc=asyncio.CancelledError()),
    )
    monkeypatch.setattr(agent_run_tasks, "get_redis", object)
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveStreamPublisher",
        FakeLiveStreamPublisher,
    )
    try:
        with capture_logs() as logs, pytest.raises(Exception) as exc_info:
            await agent_run_tasks.run_agent_answer(
                trigger=AgentRunTrigger(run_id=run.id),
                ctx=_ctx(
                    cast(
                        async_sessionmaker[AsyncSession],
                        controlled_session_factory,
                    )
                ),
            )
    finally:
        await start_session.close()
        await history_session.close()
        await cleanup_session.close()

    _assert_safe_task_boundary_error(
        exc_info.value,
        expected_message="agent run timeout terminalization failed",
    )
    _assert_sensitive_task_context_not_logged(logs)
    assert FakeLiveStreamPublisher.instances[0].published == []
    async with session_factory() as verification:
        persisted = await verification.get(AgentRun, run.id)
    assert persisted is not None
    assert (persisted.status, persisted.error_code, persisted.attempt_epoch) == (
        "running",
        None,
        1,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lower_timeout_error_follows_existing_unexpected_error_path(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    clock = _ControlledMonotonicClock(now=100.0)
    scopes = _install_application_deadline_boundary(monkeypatch, clock)
    FakeLiveStreamPublisher.instances = []
    _patch_worker_execution(
        monkeypatch,
        lambda **_kwargs: FakeAgent(exc=TimeoutError("provider timeout")),
    )
    monkeypatch.setattr(agent_run_tasks, "get_redis", object)
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveStreamPublisher",
        FakeLiveStreamPublisher,
    )

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )

    async with session_factory() as verification:
        persisted = await verification.get(AgentRun, run.id)
    assert persisted is not None
    assert (persisted.status, persisted.error_code) == ("failed", "internal_error")
    terminal = [
        event
        for event in FakeLiveStreamPublisher.instances[0].published
        if isinstance(event, AgentRunLiveStreamTerminalEvent)
    ]
    assert terminal == [
        AgentRunLiveStreamTerminalEvent(status="failed", errorCode="internal_error")
    ]
    assert scopes[0].expired() is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sweep_task_batches_queued_quota_observability_after_commit(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usage_date = date(2026, 7, 22)
    missing_counter_date = date(2026, 7, 21)
    stale_at = datetime.now(UTC) - timedelta(minutes=6)
    async with session_factory() as session:
        session.add(
            AgentUserDailyQuota(
                user_id=UUID(TEST_USER_ID),
                usage_date=usage_date,
                used_count=2,
            )
        )
        released_runs = [
            (
                await _create_thread_message_run(
                    session,
                    question=f"sensitive released queued {index}",
                    created_at=stale_at,
                    quota_usage_date=usage_date,
                )
            )[2]
            for index in range(2)
        ]
        legacy = (
            await _create_thread_message_run(
                session,
                question="sensitive legacy queued",
                created_at=stale_at,
            )
        )[2]
        inconsistent = (
            await _create_thread_message_run(
                session,
                question="sensitive inconsistent queued",
                created_at=stale_at,
                quota_usage_date=missing_counter_date,
            )
        )[2]

    metric_calls: list[dict[str, object]] = []

    def record_release(**kwargs: object) -> None:
        metric_calls.append(kwargs)

    monkeypatch.setattr(
        daily_quota_observability,
        "record_daily_quota_release",
        record_release,
    )
    FakeLiveStreamPublisher.instances = []
    monkeypatch.setattr(agent_run_tasks, "get_redis", object)
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveStreamPublisher",
        FakeLiveStreamPublisher,
    )

    with capture_logs() as logs:
        await agent_run_tasks.sweep_stale_agent_runs(ctx=_ctx(session_factory))

    batch_logs = [
        entry for entry in logs if entry.get("event") == "agent_runs_queued_stale_swept"
    ]
    assert batch_logs == [
        {
            "event": "agent_runs_queued_stale_swept",
            "log_level": "info",
            "run_count": 4,
            "quota_released_count": 2,
            "quota_not_eligible_count": 1,
            "quota_inconsistent_count": 1,
        }
    ]
    assert metric_calls == [
        {"result": "released", "count": 2},
        {"result": "not_eligible", "count": 1},
        {"result": "inconsistent", "count": 1},
    ]
    assert FakeLiveStreamPublisher.instances == []
    assert "sensitive" not in str(logs)
    async with session_factory() as session:
        counter = await session.scalar(
            select(AgentUserDailyQuota.used_count).where(
                AgentUserDailyQuota.user_id == UUID(TEST_USER_ID),
                AgentUserDailyQuota.usage_date == usage_date,
            )
        )
        statuses = [
            await session.get(AgentRun, run_id)
            for run_id in (
                *(run.id for run in released_runs),
                legacy.id,
                inconsistent.id,
            )
        ]
    assert counter == 0
    assert [
        (run.status, run.error_code) if run is not None else None for run in statuses
    ] == [("failed", "stale")] * 4


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sweep_task_publishes_each_committed_running_attempt_despite_failure(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_at = datetime.now(UTC) - timedelta(minutes=6)
    async with session_factory() as session:
        normal = (
            await _create_thread_message_run(
                session,
                status="running",
                question="sensitive normal running",
                created_at=stale_at - timedelta(minutes=1),
                started_at=stale_at,
                attempt_epoch=2,
            )
        )[2]
        missing_started = (
            await _create_thread_message_run(
                session,
                status="running",
                question="sensitive missing started",
                created_at=stale_at,
                attempt_epoch=3,
            )
        )[2]

    class CommitCheckingPublisher(FakeLiveStreamPublisher):
        async def publish(self, event: object) -> str | None:
            async with session_factory() as verification:
                persisted = await verification.get(AgentRun, self.run_id)
            assert persisted is not None
            assert (persisted.status, persisted.error_code) == ("failed", "stale")
            return await super().publish(event)

    FakeLiveStreamPublisher.instances = []
    CommitCheckingPublisher.publish_outcomes = [RuntimeError("redis unavailable"), None]
    monkeypatch.setattr(agent_run_tasks, "get_redis", object)
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveStreamPublisher",
        CommitCheckingPublisher,
    )

    with capture_logs() as logs:
        await agent_run_tasks.sweep_stale_agent_runs(ctx=_ctx(session_factory))

    assert {
        (publisher.run_id, publisher.attempt_epoch)
        for publisher in FakeLiveStreamPublisher.instances
    } == {(normal.id, 2), (missing_started.id, 3)}
    assert [
        event
        for publisher in FakeLiveStreamPublisher.instances
        for event in publisher.published
    ] == [
        AgentRunLiveStreamTerminalEvent(status="failed", errorCode="stale"),
        AgentRunLiveStreamTerminalEvent(status="failed", errorCode="stale"),
    ]
    assert [
        entry for entry in logs if entry.get("event") == "running_timeout_swept"
    ] == [{"count": 2, "event": "running_timeout_swept", "log_level": "info"}]
    assert [
        entry for entry in logs if entry.get("event") == "running_without_started_at"
    ] == [{"count": 1, "event": "running_without_started_at", "log_level": "warning"}]
    assert (
        len(
            [
                entry
                for entry in logs
                if entry.get("event") == "agent_run_live_stream_terminal_publish_failed"
            ]
        )
        == 1
    )
    assert "sensitive" not in str(logs)
    assert await _status_for_sweep_test(session_factory, normal.id) == (
        "failed",
        "stale",
    )
    assert await _status_for_sweep_test(session_factory, missing_started.id) == (
        "failed",
        "stale",
    )


async def _status_for_sweep_test(
    session_factory: async_sessionmaker[AsyncSession], run_id: UUID
) -> tuple[str, str | None]:
    async with session_factory() as session:
        run = await session.get(AgentRun, run_id)
    assert run is not None
    return run.status, run.error_code


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sweep_task_emits_no_queued_result_or_event_when_commit_rolls_back(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usage_date = date(2026, 7, 22)
    async with session_factory() as setup_session:
        _thread, _message, run = await _create_thread_message_run(
            setup_session,
            question="sensitive rollback queued",
            created_at=datetime.now(UTC) - timedelta(minutes=6),
            quota_usage_date=usage_date,
        )
        setup_session.add(
            AgentUserDailyQuota(
                user_id=UUID(TEST_USER_ID),
                usage_date=usage_date,
                used_count=1,
            )
        )
        await setup_session.commit()

    metric_calls: list[dict[str, object]] = []
    failing_session = session_factory()

    def fail_commit(_session: object) -> None:
        raise SensitivePersistenceFailure

    def record_release(**kwargs: object) -> None:
        metric_calls.append(kwargs)

    sa_event.listen(
        failing_session.sync_session,
        "before_commit",
        fail_commit,
        once=True,
    )
    monkeypatch.setattr(
        daily_quota_observability,
        "record_daily_quota_release",
        record_release,
    )

    def failing_session_factory() -> AsyncSession:
        return failing_session

    try:
        with (
            capture_logs() as logs,
            pytest.raises(Exception) as exc_info,
        ):
            await agent_run_tasks.sweep_stale_agent_runs(
                ctx=_ctx(
                    cast(
                        async_sessionmaker[AsyncSession],
                        failing_session_factory,
                    )
                )
            )
    finally:
        await failing_session.close()

    _assert_safe_task_boundary_error(
        exc_info.value,
        expected_message="agent run stale sweep failed",
    )
    _assert_sensitive_task_context_not_logged(logs)
    assert metric_calls == []
    assert not [
        entry
        for entry in logs
        if entry.get("event")
        in {
            "agent_runs_queued_stale_swept",
            "running_timeout_swept",
            "running_without_started_at",
        }
    ]
    assert await _status_for_sweep_test(session_factory, run.id) == ("queued", None)
