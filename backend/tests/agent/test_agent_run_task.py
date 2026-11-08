"""Agent run worker の1回の実行の契約。

実DBのRunを、差し替えた回答工程と live stream publisher で実行する。
taskiq の宣言のテストだけはDBを使わない。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

import app.agent.run_execution as run_execution
import app.queue.tasks.agent_run as agent_run_tasks
from app.agent.answering import timing as answer_timing
from app.agent.answering.direct_answer.contract import DirectAnswerInvalidError
from app.agent.answering.direct_answer.failure import DirectAnswerError
from app.agent.answering.evidence_answer.failure import EvidenceAnswerError
from app.agent.answering_runner import (
    AnsweringRunError,
    RunIdentity,
    RunInput,
    RunResult,
)
from app.agent.contract import (
    AnswerPlanSummary,
    AnswerQuestionResult,
    ExternalUrlSource,
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
from app.agent.output import AgentOutputDefect, AgentOutputInvalidError
from app.agent.planning.failure import PlanningError
from app.agent.research_handoff import (
    ResearchHandoff,
    ResearchRunRecord,
    ResearchTaskRecord,
)
from app.agent.run_enqueuer import AgentRunTrigger
from app.agent.running.daily_quota.release import DailyQuotaReleaseOutcome
from app.agent.running.state import (
    AnswerGenerationStarted,
    RunFinished,
    RunRejected,
    RunRejectionReason,
)
from app.agent.running.state_repository import AgentRunStateRepository
from app.agent.running.stop import AnswerGenerationStopped
from app.agent.running.types import AgentRunErrorCode, AgentRunStatus
from app.agent.threads.contracts import ThreadMessageSnapshot
from app.agent.threads.repository import AgentThreadRepository
from app.ai_providers.errors import (
    AIProviderNotSentError,
    AIProviderNotSentReason,
    AIProviderResponseError,
    AIProviderResponseReason,
    AIProviderResultError,
    AIProviderResultReason,
)
from app.http.errors import HttpResponseError
from app.models.agent_message import AgentMessage, AgentMessageSource
from app.models.agent_run import AgentRun
from app.models.agent_thread import AgentThread
from app.models.agent_user_daily_quota import AgentUserDailyQuota
from app.shared.web_url import WebUrl
from tests.agent._sensitive_db_failure import (
    SensitivePersistenceFailure,
    assert_no_sensitive_marker,
)
from tests.agent.running._seed import (
    create_thread_message_run as _create_thread_message_run,
)
from tests.agent.running._start_run_outcomes import (
    started_attempt_epoch,
)
from tests.conftest import TEST_USER_ID

_RECEIVED_AT = datetime(2026, 1, 1, tzinfo=UTC)


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
        self.repository: object | None = None
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
        assert self.repository is not None
        await cast(Any, self.repository).start_answer_generation()
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

    async def ensure_can_continue(self) -> None:
        return None


class CapturingAnswerStart:
    instances: list[CapturingAnswerStart] = []

    def __init__(
        self,
        session_factory: object,
        run_id: UUID,
        attempt_epoch: int,
    ) -> None:
        self.session_factory = session_factory
        self.run_id = run_id
        self.attempt_epoch = attempt_epoch
        CapturingAnswerStart.instances.append(self)

    async def start_answer_generation(self) -> AnswerGenerationStarted:
        return AnswerGenerationStarted(self.run_id, datetime.now(UTC))


class ForbiddenConstruction:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("start skip後にlive dependencyを生成してはいけません")


def _ctx(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    agent_live_redis: object = object(),
) -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(
            session_factory=session_factory,
            agent_live_redis=agent_live_redis,
            agent_deadline_scheduler=SimpleNamespace(
                reserve_in_background=lambda *_: None
            ),
        )
    )


def _patch_worker_execution(
    monkeypatch: pytest.MonkeyPatch,
    execution_builder: object,
    *,
    answering_runner: FakeAnsweringRunner | None = None,
) -> FakeAnsweringRunner:
    answering_runner = answering_runner or FakeAnsweringRunner()

    def build_runner(**kwargs: object) -> FakeAnsweringRunner:
        answering_runner.execution = cast(Any, execution_builder)(**kwargs)
        answering_runner.repository = kwargs["repository"]
        return answering_runner

    monkeypatch.setattr(
        run_execution,
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
        run_execution,
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
                url=WebUrl("https://example.com/agent-source"),
                title="Agent source",
                evidence_claim="Agent claim.",
                source_name="Example",
            )
        ],
        missing_aspects=[],
        plan_summary=_plan_summary("search"),
    )


@pytest.mark.asyncio
async def test_run_agent_answer_completes_run_and_persists_assistant_message(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回答できたRunは開始した世代で完了し、回答・出典・threadの更新時刻を保存する。"""
    async with session_factory() as session:
        thread, _message, run = await _create_thread_message_run(session)
    fake_agent = FakeAgent(_external_result())
    persisted_results: list[AnswerQuestionResult] = []
    completed_epochs: list[int] = []

    class CapturingStateRepository(AgentRunStateRepository):
        async def complete(
            self,
            run_id: UUID,
            *,
            attempt_epoch: int,
            result: AnswerQuestionResult,
            research_handoff: dict[str, Any] | None = None,
            now: datetime | None = None,
        ) -> RunFinished | RunRejected:
            persisted_results.append(result)
            completed_epochs.append(attempt_epoch)
            return await super().complete(
                run_id,
                attempt_epoch=attempt_epoch,
                result=result,
                research_handoff=research_handoff,
                now=now,
            )

    answering_runner = _patch_worker_execution(
        monkeypatch,
        lambda **_kwargs: fake_agent,
    )
    monkeypatch.setattr(
        run_execution,
        "AgentRunStateRepository",
        CapturingStateRepository,
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


def _capture_completion_handoffs(
    monkeypatch: pytest.MonkeyPatch,
) -> list[dict[str, Any] | None]:
    """完了の保存へ渡る handoff payload を記録しつつ、本物へ委譲する。"""
    captured: list[dict[str, Any] | None] = []

    class CapturingStateRepository(AgentRunStateRepository):
        async def complete(
            self,
            run_id: UUID,
            *,
            attempt_epoch: int,
            result: AnswerQuestionResult,
            research_handoff: dict[str, Any] | None = None,
            now: datetime | None = None,
        ) -> RunFinished | RunRejected:
            captured.append(research_handoff)
            return await super().complete(
                run_id,
                attempt_epoch=attempt_epoch,
                result=result,
                research_handoff=research_handoff,
                now=now,
            )

    monkeypatch.setattr(
        run_execution,
        "AgentRunStateRepository",
        CapturingStateRepository,
    )
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
async def test_run_agent_answer_forwards_the_serialized_handoff_to_completion(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RunResult.research_handoffはmodel_dump(mode="json")のdictへ変換されてから

    完了の保存へ渡る(agent_threads.research_handoffの正本はJSON snake_caseで
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
    captured_handoffs = _capture_completion_handoffs(monkeypatch)

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
    """記録を追加しなかったRunはNoneのまま完了の保存へ渡る。"""
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    fake_agent = FakeAgent(_direct_result())
    _patch_worker_execution(
        monkeypatch,
        lambda **_kwargs: fake_agent,
        answering_runner=FakeAnsweringRunner(research_handoff=None),
    )
    captured_handoffs = _capture_completion_handoffs(monkeypatch)

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
    """model_dump失敗は完了の保存より前に完結し、Noneとして渡る。

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
    captured_handoffs = _capture_completion_handoffs(monkeypatch)

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
        run_execution,
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
    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory, agent_live_redis=redis),
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
    monkeypatch.setattr(
        run_execution,
        "AgentRunLiveStreamPublisher",
        FakeLiveStreamPublisher,
        raising=False,
    )

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory, agent_live_redis=redis),
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
    CapturingAnswerStart.instances = []

    def build_agent(**kwargs: object) -> FakeAgent:
        captured_kwargs.update(kwargs)
        return fake_agent

    _patch_worker_execution(monkeypatch, build_agent)
    monkeypatch.setattr(
        run_execution,
        "AgentRunLiveStreamPublisher",
        FakeLiveStreamPublisher,
    )
    monkeypatch.setattr(
        run_execution,
        "AgentRunLiveAnswerDeltaReporter",
        CapturingDeltaReporter,
        raising=False,
    )
    monkeypatch.setattr(
        run_execution,
        "AgentRunExecutionProbe",
        CapturingExecutionProbe,
        raising=False,
    )
    monkeypatch.setattr(
        run_execution,
        "AgentAnswerGenerationRepository",
        CapturingAnswerStart,
        raising=False,
    )

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory, agent_live_redis=redis),
    )

    assert len(CapturingDeltaReporter.instances) == 1
    assert len(CapturingExecutionProbe.instances) == 1
    assert len(CapturingAnswerStart.instances) == 1
    stream = FakeLiveStreamPublisher.instances[0]
    delta_reporter = CapturingDeltaReporter.instances[0]
    probe = CapturingExecutionProbe.instances[0]
    answer_start = CapturingAnswerStart.instances[0]
    assert delta_reporter.publisher is stream
    assert delta_reporter.run_id == run.id
    assert delta_reporter.attempt_epoch == 1
    assert probe.session_factory is session_factory
    assert probe.run_id == run.id
    assert probe.attempt_epoch == 1
    assert answer_start.session_factory is session_factory
    assert answer_start.run_id == run.id
    assert answer_start.attempt_epoch == 1
    assert isinstance(captured_kwargs["progress"], AgentRunLiveStageReporter)
    assert captured_kwargs["delta_reporter"] is delta_reporter
    assert "continuation" not in captured_kwargs
    assert captured_kwargs["repository"] is answer_start


@pytest.mark.asyncio
async def test_answer_start_deadline_publishes_existing_terminal_event(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回答開始で受理期限を過ぎていたRunは期限切れで終わり、その世代へ終了を通知する。"""
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    FakeLiveStreamPublisher.instances = []

    class DeadlineAtAnswerStartRunner:
        def __init__(self, answer_start: object) -> None:
            self._answer_start = answer_start

        async def run(self, input: RunInput, *, identity: RunIdentity) -> RunResult:
            del input, identity
            async with session_factory() as session:
                async with session.begin():
                    persisted = await session.get(AgentRun, run.id)
                    assert persisted is not None
                    persisted.deadline_at = datetime.now(UTC) - timedelta(seconds=1)
            with pytest.raises(AnswerGenerationStopped) as stopped:
                await cast(Any, self._answer_start).start_answer_generation()
            assert stopped.value.outcome == RunFinished(
                status=AgentRunStatus.DEADLINE_EXCEEDED,
                error_code=None,
                quota_release_outcome=DailyQuotaReleaseOutcome.NOT_ELIGIBLE,
                running_attempt_epoch=1,
            )
            raise stopped.value

    def build_runner(**kwargs: object) -> DeadlineAtAnswerStartRunner:
        return DeadlineAtAnswerStartRunner(kwargs["repository"])

    monkeypatch.setattr(
        run_execution,
        "build_answering_runner",
        build_runner,
        raising=False,
    )
    monkeypatch.setattr(
        run_execution,
        "AgentRunExecutionProbe",
        CapturingExecutionProbe,
        raising=False,
    )
    monkeypatch.setattr(
        run_execution,
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
    assert terminal == [
        AgentRunLiveStreamTerminalEvent(status="deadline_exceeded"),
    ]
    async with session_factory() as session:
        persisted = await session.get(AgentRun, run.id)
    assert persisted is not None
    assert persisted.status == "deadline_exceeded"
    assert persisted.answer_started_at is None


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
        run_execution,
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
    CapturingAnswerStart.instances = []

    def forbidden_builder(*_args: object, **_kwargs: object) -> None:
        pytest.fail(
            "start skip後にexecution dependencyをbuildしてはいけません",
        )

    monkeypatch.setattr(
        run_execution,
        "build_answering_runner",
        forbidden_builder,
        raising=False,
    )
    monkeypatch.setattr(
        run_execution,
        "AgentRunLiveStreamPublisher",
        FakeLiveStreamPublisher,
        raising=False,
    )
    monkeypatch.setattr(
        run_execution,
        "AgentRunLiveAnswerDeltaReporter",
        ForbiddenConstruction,
        raising=False,
    )
    monkeypatch.setattr(
        run_execution,
        "AgentRunExecutionProbe",
        ForbiddenConstruction,
        raising=False,
    )
    monkeypatch.setattr(
        run_execution,
        "AgentAnswerGenerationRepository",
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
    assert CapturingAnswerStart.instances == []


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
            run_execution,
            legacy_name,
            fail_if_legacy_semantic_owner_is_called,
            raising=False,
        )
    monkeypatch.setattr(run_execution, "datetime", FixedDateTime)
    monkeypatch.setattr(
        run_execution,
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
    assert runner_kwargs["repository"] is not None
    assert "continuation" not in runner_kwargs
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
        run_execution,
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
async def test_completion_deadline_publishes_after_commit_and_survives_publish_failure(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    fake_agent = FakeAgent(_direct_result())
    FakeLiveStreamPublisher.instances = []
    monkeypatch.setattr(answer_timing, "ANSWER_GENERATION_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(answer_timing, "ANSWER_GENERATION_RECOVERY_GRACE_SECONDS", 0)
    monkeypatch.setattr(FakeLiveStreamPublisher, "raise_on_publish", True)

    class CommitCheckingPublisher(FakeLiveStreamPublisher):
        async def publish(self, event: object) -> str | None:
            if isinstance(event, AgentRunLiveStreamTerminalEvent):
                async with session_factory() as session:
                    persisted = await session.get(AgentRun, run.id)
                    assert persisted is not None
                    assert persisted.status == "deadline_exceeded"
                    assert persisted.assistant_message_id is None
            return await super().publish(event)

    _patch_worker_execution(monkeypatch, lambda **_kwargs: fake_agent)
    monkeypatch.setattr(
        run_execution,
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
    assert terminal == [AgentRunLiveStreamTerminalEvent(status="deadline_exceeded")]
    async with session_factory() as session:
        persisted = await session.get(AgentRun, run.id)
        assert persisted is not None
        assert persisted.status == "deadline_exceeded"
        assert persisted.assistant_message_id is None


@pytest.mark.asyncio
async def test_run_agent_answer_publishes_failed_terminal_after_commit(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    fake_agent = FakeAgent(
        exc=AnsweringRunError(
            cause=AIProviderResponseError(
                reason=AIProviderResponseReason.SERVER_ERROR,
                http_error=HttpResponseError(status_code=503, received_at=_RECEIVED_AT),
            )
        )
    )
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
        run_execution,
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
    read_logs,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """古い世代として止められた回答工程は、Runを変えず終了も通知せずに戻る。"""
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    fake_agent = FakeAgent(
        exc=AnswerGenerationStopped(
            RunRejected(
                reason=RunRejectionReason.ATTEMPT_SUPERSEDED,
                observed_status=AgentRunStatus.RUNNING,
            )
        )
    )
    complete_calls: list[tuple[UUID, int]] = []
    mark_failed_calls: list[tuple[UUID, int]] = []

    class ObservingStateRepository(AgentRunStateRepository):
        async def complete(
            self,
            run_id: UUID,
            *,
            attempt_epoch: int,
            result: AnswerQuestionResult,
            research_handoff: dict[str, Any] | None = None,
            now: datetime | None = None,
        ) -> RunFinished | RunRejected:
            assert result == fake_agent.result
            complete_calls.append((run_id, attempt_epoch))
            return RunRejected(
                reason=RunRejectionReason.ATTEMPT_SUPERSEDED,
                observed_status=AgentRunStatus.RUNNING,
            )

        async def record_failure(
            self,
            run_id: UUID,
            *,
            attempt_epoch: int,
            error_code: AgentRunErrorCode,
            now: datetime | None = None,
        ) -> RunFinished | RunRejected:
            assert error_code == AgentRunErrorCode.INTERNAL_ERROR
            mark_failed_calls.append((run_id, attempt_epoch))
            return RunRejected(
                reason=RunRejectionReason.ATTEMPT_SUPERSEDED,
                observed_status=AgentRunStatus.RUNNING,
            )

    _patch_delta_worker(
        monkeypatch,
        lambda **_kwargs: fake_agent,
    )
    monkeypatch.setattr(
        run_execution,
        "AgentRunStateRepository",
        ObservingStateRepository,
    )

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )
    logs = read_logs()

    assert complete_calls == []
    assert mark_failed_calls == []
    stop_logs = [
        entry for entry in logs if entry.get("event") == "agent_run_generation_stopped"
    ]
    assert len(stop_logs) == 1
    assert stop_logs[0]["level"] == "info"
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
async def test_epoch_advance_before_answer_start_stops_old_worker_through_actual_probe(
    read_logs,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回答開始前に世代が進むと、古いworkerは実際の継続確認で止まり、Runを変えない。"""
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)

    class ManualClock:
        def __init__(self) -> None:
            self.now = 0.0

        def __call__(self) -> float:
            return self.now

    clock = ManualClock()
    production_probe_type = run_execution.AgentRunExecutionProbe
    probe_bindings: list[tuple[object, UUID, int]] = []
    complete_calls: list[tuple[UUID, int]] = []
    mark_failed_calls: list[tuple[UUID, int]] = []
    probes: list[object] = []

    def build_probe(
        bound_session_factory: object,
        run_id: UUID,
        attempt_epoch: int,
    ) -> object:
        probe_bindings.append((bound_session_factory, run_id, attempt_epoch))
        probe = production_probe_type(
            bound_session_factory,
            run_id,
            attempt_epoch,
            clock=clock,
        )
        probes.append(probe)
        return probe

    class EpochAdvancingRunner:
        def __init__(self) -> None:
            self.continuation: object | None = None

        async def run(self, input: RunInput, *, identity: RunIdentity) -> RunResult:
            assert self.continuation is not None
            await self.continuation.ensure_can_continue()  # type: ignore[attr-defined]
            async with session_factory() as restart_session:
                async with restart_session.begin():
                    attempt_epoch = started_attempt_epoch(
                        await AgentRunStateRepository(restart_session).start_attempt(
                            run.id
                        )
                    )
            assert attempt_epoch == 2
            clock.now = 2.0
            with pytest.raises(AnswerGenerationStopped) as stopped:
                await self.continuation.ensure_can_continue()  # type: ignore[attr-defined]
            assert stopped.value.outcome == RunRejected(
                reason=RunRejectionReason.ATTEMPT_SUPERSEDED,
                observed_status=AgentRunStatus.RUNNING,
            )
            raise stopped.value

    fake_runner = EpochAdvancingRunner()

    def build_runner(**kwargs: object) -> EpochAdvancingRunner:
        del kwargs
        fake_runner.continuation = probes[0]
        return fake_runner

    class ObservingStateRepository(AgentRunStateRepository):
        async def complete(
            self,
            run_id: UUID,
            *,
            attempt_epoch: int,
            result: AnswerQuestionResult,
            research_handoff: dict[str, Any] | None = None,
            now: datetime | None = None,
        ) -> RunFinished | RunRejected:
            complete_calls.append((run_id, attempt_epoch))
            return RunRejected(
                reason=RunRejectionReason.ATTEMPT_SUPERSEDED,
                observed_status=AgentRunStatus.RUNNING,
            )

        async def record_failure(
            self,
            run_id: UUID,
            *,
            attempt_epoch: int,
            error_code: AgentRunErrorCode,
            now: datetime | None = None,
        ) -> RunFinished | RunRejected:
            assert error_code == AgentRunErrorCode.INTERNAL_ERROR
            mark_failed_calls.append((run_id, attempt_epoch))
            return RunRejected(
                reason=RunRejectionReason.ATTEMPT_SUPERSEDED,
                observed_status=AgentRunStatus.RUNNING,
            )

    FakeLiveStreamPublisher.instances = []
    monkeypatch.setattr(FakeLiveStreamPublisher, "publish_outcomes", [])
    monkeypatch.setattr(
        run_execution, "AgentRunLiveStreamPublisher", FakeLiveStreamPublisher
    )
    monkeypatch.setattr(run_execution, "build_answering_runner", build_runner)
    monkeypatch.setattr(run_execution, "AgentRunExecutionProbe", build_probe)
    monkeypatch.setattr(
        run_execution,
        "AgentRunStateRepository",
        ObservingStateRepository,
    )

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )
    logs = read_logs()

    assert probe_bindings == [(session_factory, run.id, 1)]
    assert complete_calls == []
    assert mark_failed_calls == []
    stop_logs = [
        entry for entry in logs if entry.get("event") == "agent_run_generation_stopped"
    ]
    assert len(stop_logs) == 1
    assert stop_logs[0]["level"] == "info"
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
async def test_deadline_stop_publishes_terminal_without_mark_failed(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """継続確認で期限切れになったRunは失敗を記録せず終了を通知し、利用枠を返さない。"""
    usage_date = date(2026, 9, 2)
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

    class ManualClock:
        def __init__(self) -> None:
            self.now = 0.0

        def __call__(self) -> float:
            return self.now

    clock = ManualClock()
    production_probe_type = run_execution.AgentRunExecutionProbe
    mark_failed_calls: list[tuple[UUID, int]] = []
    probes: list[object] = []

    def build_probe(
        bound_session_factory: object,
        run_id: UUID,
        attempt_epoch: int,
    ) -> object:
        probe = production_probe_type(
            bound_session_factory,
            run_id,
            attempt_epoch,
            clock=clock,
        )
        probes.append(probe)
        return probe

    class DeadlineExpiringAgent:
        def __init__(self) -> None:
            self.continuation: object | None = None

        async def answer(self) -> AnswerQuestionResult:
            assert self.continuation is not None
            async with session_factory() as expire_session:
                async with expire_session.begin():
                    persisted = await expire_session.get(AgentRun, run.id)
                    assert persisted is not None
                    persisted.deadline_at = datetime.now(UTC) - timedelta(seconds=1)
            clock.now = 2.0
            with pytest.raises(AnswerGenerationStopped) as stopped:
                await self.continuation.ensure_can_continue()  # type: ignore[attr-defined]
            assert stopped.value.outcome == RunFinished(
                status=AgentRunStatus.DEADLINE_EXCEEDED,
                error_code=None,
                quota_release_outcome=DailyQuotaReleaseOutcome.NOT_ELIGIBLE,
                running_attempt_epoch=1,
            )
            raise stopped.value

    fake_agent = DeadlineExpiringAgent()

    def build_agent(**kwargs: object) -> DeadlineExpiringAgent:
        del kwargs
        fake_agent.continuation = probes[0]
        return fake_agent

    class ObservingStateRepository(AgentRunStateRepository):
        async def record_failure(
            self,
            run_id: UUID,
            *,
            attempt_epoch: int,
            error_code: AgentRunErrorCode,
            now: datetime | None = None,
        ) -> RunFinished | RunRejected:
            mark_failed_calls.append((run_id, attempt_epoch))
            return RunRejected(
                reason=RunRejectionReason.ALREADY_FINISHED,
                observed_status=AgentRunStatus.DEADLINE_EXCEEDED,
            )

    _patch_delta_worker(monkeypatch, build_agent)
    monkeypatch.setattr(run_execution, "AgentRunExecutionProbe", build_probe)
    monkeypatch.setattr(
        run_execution,
        "AgentRunStateRepository",
        ObservingStateRepository,
    )

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )

    assert mark_failed_calls == []
    terminal = [
        event
        for event in FakeLiveStreamPublisher.instances[0].published
        if isinstance(event, AgentRunLiveStreamTerminalEvent)
    ]
    assert terminal == [
        AgentRunLiveStreamTerminalEvent(status="deadline_exceeded"),
    ]
    async with session_factory() as session:
        persisted = await session.get(AgentRun, run.id)
        used_count = await session.scalar(
            select(AgentUserDailyQuota.used_count).where(
                AgentUserDailyQuota.user_id == UUID(TEST_USER_ID),
                AgentUserDailyQuota.usage_date == usage_date,
            )
        )
    assert persisted is not None
    assert persisted.status == "deadline_exceeded"
    assert persisted.attempt_epoch == 1
    assert persisted.error_code is None
    assert used_count == 1


@pytest.mark.asyncio
async def test_delta_finish_precedes_completed_commit_and_terminal(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回答の差分を送り終えてから完了を保存し、完了の終了通知を最後に送る。"""
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    order: list[str] = []
    fake_agent = DeltaReportingAgent(
        result=_direct_result(),
        fragments=["D" * 512],
        order=order,
    )

    def build_agent(**kwargs: object) -> DeltaReportingAgent:
        fake_agent.delta_reporter = kwargs["delta_reporter"]
        assert kwargs["repository"] is not None
        return fake_agent

    class ObservingStateRepository(AgentRunStateRepository):
        async def complete(
            self,
            run_id: UUID,
            *,
            attempt_epoch: int,
            result: AnswerQuestionResult,
            research_handoff: dict[str, Any] | None = None,
            now: datetime | None = None,
        ) -> RunFinished | RunRejected:
            order.append("complete_start")
            assert attempt_epoch == 1
            return await super().complete(
                run_id,
                attempt_epoch=attempt_epoch,
                result=result,
                research_handoff=research_handoff,
                now=now,
            )

    _patch_delta_worker(monkeypatch, build_agent)
    monkeypatch.setattr(
        run_execution,
        "AgentRunStateRepository",
        ObservingStateRepository,
    )

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
        fake_agent.continuation = kwargs["repository"]
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
    assert captured_controls["repository"] is fake_agent.continuation


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
        exc=AnsweringRunError(
            cause=AIProviderResultError(reason=AIProviderResultReason.STREAM_INCOMPLETE)
        ),
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


@pytest.mark.asyncio
async def test_completion_loser_with_existing_delta_has_no_terminal_or_assistant(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """差分を送った後に完了が拒否されても、終了を通知せず回答も保存しない。"""
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    fake_agent = DeltaReportingAgent(
        result=_direct_result(),
        fragments=["L" * 512],
    )

    def build_agent(**kwargs: object) -> DeltaReportingAgent:
        fake_agent.delta_reporter = kwargs["delta_reporter"]
        return fake_agent

    class RejectingStateRepository(AgentRunStateRepository):
        async def complete(
            self,
            run_id: UUID,
            *,
            attempt_epoch: int,
            result: AnswerQuestionResult,
            research_handoff: dict[str, Any] | None = None,
            now: datetime | None = None,
        ) -> RunFinished | RunRejected:
            assert (run_id, result, attempt_epoch) == (
                run.id,
                fake_agent.result,
                1,
            )
            return RunRejected(
                reason=RunRejectionReason.ALREADY_FINISHED,
                observed_status=AgentRunStatus.FAILED,
            )

    _patch_delta_worker(monkeypatch, build_agent)
    monkeypatch.setattr(
        run_execution,
        "AgentRunStateRepository",
        RejectingStateRepository,
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
async def test_completion_failure_publishes_failed_terminal(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回答の保存に失敗したときは失敗の終端通知を出す。"""
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    fake_agent = FakeAgent(_direct_result())
    FakeLiveStreamPublisher.instances = []

    class FailingStateRepository(AgentRunStateRepository):
        async def complete(
            self,
            run_id: UUID,
            *,
            attempt_epoch: int,
            result: AnswerQuestionResult,
            research_handoff: dict[str, Any] | None = None,
            now: datetime | None = None,
        ) -> RunFinished | RunRejected:
            raise RuntimeError("completion failed")

    _patch_worker_execution(monkeypatch, lambda **_kwargs: fake_agent)
    monkeypatch.setattr(
        run_execution,
        "AgentRunLiveStreamPublisher",
        FakeLiveStreamPublisher,
    )
    monkeypatch.setattr(
        run_execution,
        "AgentRunStateRepository",
        FailingStateRepository,
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
            errorCode="internal_error",
        )
    ]


@pytest.mark.parametrize(
    "rejection",
    [
        RunRejected(
            reason=RunRejectionReason.RUN_NOT_FOUND,
            observed_status=None,
        ),
        RunRejected(
            reason=RunRejectionReason.ALREADY_FINISHED,
            observed_status=AgentRunStatus.FAILED,
        ),
        RunRejected(
            reason=RunRejectionReason.ATTEMPT_SUPERSEDED,
            observed_status=AgentRunStatus.QUEUED,
        ),
        RunRejected(
            reason=RunRejectionReason.ATTEMPT_SUPERSEDED,
            observed_status=AgentRunStatus.RUNNING,
        ),
        RunRejected(
            reason=RunRejectionReason.ANSWER_NOT_STARTED,
            observed_status=AgentRunStatus.RUNNING,
        ),
    ],
    ids=(
        "run-not-found",
        "already-finished",
        "superseded-from-queued",
        "superseded-from-running",
        "answer-not-started",
    ),
)
@pytest.mark.asyncio
async def test_completion_skip_does_not_publish_terminal(
    rejection: RunRejected,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """完了が拒否されたときは終了を通知しない。"""
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    fake_agent = FakeAgent(_direct_result())
    FakeLiveStreamPublisher.instances = []

    class RejectingStateRepository(AgentRunStateRepository):
        async def complete(
            self,
            run_id: UUID,
            *,
            attempt_epoch: int,
            result: AnswerQuestionResult,
            research_handoff: dict[str, Any] | None = None,
            now: datetime | None = None,
        ) -> RunFinished | RunRejected:
            assert (run_id, result, attempt_epoch) == (
                run.id,
                fake_agent.result,
                1,
            )
            return rejection

    _patch_worker_execution(monkeypatch, lambda **_kwargs: fake_agent)
    monkeypatch.setattr(
        run_execution,
        "AgentRunLiveStreamPublisher",
        FakeLiveStreamPublisher,
    )
    monkeypatch.setattr(
        run_execution,
        "AgentRunStateRepository",
        RejectingStateRepository,
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


@pytest.mark.asyncio
async def test_failed_transition_loser_does_not_publish_terminal(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """失敗の記録が拒否されたときは終了を通知しない。"""
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    fake_agent = FakeAgent(
        exc=AnsweringRunError(
            cause=AIProviderResponseError(
                reason=AIProviderResponseReason.SERVER_ERROR,
                http_error=HttpResponseError(status_code=503, received_at=_RECEIVED_AT),
            )
        )
    )
    FakeLiveStreamPublisher.instances = []

    class RejectingStateRepository(AgentRunStateRepository):
        async def record_failure(
            self,
            run_id: UUID,
            *,
            attempt_epoch: int,
            error_code: AgentRunErrorCode,
            now: datetime | None = None,
        ) -> RunFinished | RunRejected:
            assert (run_id, error_code, attempt_epoch) == (
                run.id,
                AgentRunErrorCode.GENERATION_UNAVAILABLE,
                1,
            )
            return RunRejected(
                reason=RunRejectionReason.ATTEMPT_SUPERSEDED,
                observed_status=AgentRunStatus.RUNNING,
            )

    _patch_worker_execution(monkeypatch, lambda **_kwargs: fake_agent)
    monkeypatch.setattr(
        run_execution,
        "AgentRunLiveStreamPublisher",
        FakeLiveStreamPublisher,
    )
    monkeypatch.setattr(
        run_execution,
        "AgentRunStateRepository",
        RejectingStateRepository,
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
        run_execution,
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
@pytest.mark.parametrize(
    "exc",
    [
        AnsweringRunError(
            cause=AIProviderResponseError(
                reason=AIProviderResponseReason.AUTH,
                http_error=HttpResponseError(status_code=401, received_at=_RECEIVED_AT),
            )
        ),
        AnsweringRunError(
            cause=AIProviderNotSentError(reason=AIProviderNotSentReason.NOT_CONFIGURED)
        ),
    ],
)
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
@pytest.mark.parametrize(
    ("generation_error", "expected_exception"),
    [
        pytest.param(
            AnsweringRunError(
                cause=AIProviderNotSentError(
                    reason=AIProviderNotSentReason.NOT_CONFIGURED
                )
            ),
            {
                "error_class": "app.agent.answering_runner.AnsweringRunError",
                "error_message": "リサーチの実行が工程の失敗で止まりました",
                "related_exceptions": [
                    {
                        "parent": None,
                        "relation": "cause",
                        "exception": {
                            "error_class": (
                                "app.ai_providers.errors.AIProviderNotSentError"
                            ),
                            "error_message": (
                                "AIプロバイダーへのリクエストを送信しませんでした"
                            ),
                            "error_details": {
                                "code": "ai_provider_not_sent_error",
                                "reason": "not_configured",
                            },
                        },
                    }
                ],
            },
            id="runtime-not-configured",
        ),
        pytest.param(
            AnsweringRunError(
                cause=PlanningError(
                    cause=AIProviderResponseError(
                        reason=AIProviderResponseReason.AUTH,
                        http_error=HttpResponseError(
                            status_code=401, received_at=_RECEIVED_AT
                        ),
                    )
                )
            ),
            {
                "error_class": "app.agent.answering_runner.AnsweringRunError",
                "error_message": "リサーチの実行が工程の失敗で止まりました",
                "related_exceptions": [
                    {
                        "parent": None,
                        "relation": "cause",
                        "exception": {
                            "error_class": "app.agent.planning.failure.PlanningError",
                            "error_message": "質問の計画を作れませんでした",
                            "error_details": {"code": "ai_provider_response_error"},
                        },
                    },
                    {
                        "parent": 0,
                        "relation": "cause",
                        "exception": {
                            "error_class": (
                                "app.ai_providers.errors.AIProviderResponseError"
                            ),
                            "error_message": "AIプロバイダーが失敗の応答を返しました",
                            "error_details": {
                                "code": "ai_provider_response_error",
                                "reason": "auth",
                            },
                        },
                    },
                ],
            },
            id="planning-provider",
        ),
        pytest.param(
            AnsweringRunError(
                cause=DirectAnswerError(cause=DirectAnswerInvalidError())
            ),
            {
                "error_class": "app.agent.answering_runner.AnsweringRunError",
                "error_message": "リサーチの実行が工程の失敗で止まりました",
                "related_exceptions": [
                    {
                        "parent": None,
                        "relation": "cause",
                        "exception": {
                            "error_class": (
                                "app.agent.answering.direct_answer.failure."
                                "DirectAnswerError"
                            ),
                            "error_message": "直接回答を作れませんでした",
                            "error_details": {"code": "direct_answer_blank_response"},
                        },
                    },
                    {
                        "parent": 0,
                        "relation": "cause",
                        "exception": {
                            "error_class": (
                                "app.agent.answering.direct_answer.contract."
                                "DirectAnswerInvalidError"
                            ),
                            "error_message": "直接回答の応答が空でした",
                        },
                    },
                ],
            },
            id="direct-answer-blank",
        ),
        pytest.param(
            AnsweringRunError(cause=EvidenceAnswerError(cause=TimeoutError())),
            {
                "error_class": "app.agent.answering_runner.AnsweringRunError",
                "error_message": "リサーチの実行が工程の失敗で止まりました",
                "related_exceptions": [
                    {
                        "parent": None,
                        "relation": "cause",
                        "exception": {
                            "error_class": (
                                "app.agent.answering.evidence_answer.failure."
                                "EvidenceAnswerError"
                            ),
                            "error_message": "根拠付き回答を作れませんでした",
                            "error_details": {"code": "evidence_answer_timeout"},
                        },
                    },
                    {
                        "parent": 0,
                        "relation": "cause",
                        "exception": {
                            "error_class": "builtins.TimeoutError",
                            "error_message": "",
                        },
                    },
                ],
            },
            id="evidence-answer-timeout",
        ),
    ],
)
async def test_run_agent_answer_generation_error_marks_failed_and_logs_cause(
    read_logs,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    generation_error: Exception,
    expected_exception: dict[str, object],
) -> None:
    """既知の生成失敗はrunを生成不能で終え、例外の型・説明・原因をinfoで記録する。"""
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    fake_agent = FakeAgent(exc=generation_error)
    _patch_worker_execution(monkeypatch, lambda **_kwargs: fake_agent)

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )
    logs = read_logs()

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
    assert [
        entry
        for entry in logs
        if entry.get("event") == "agent_run_generation_unavailable"
    ] == [
        {
            "event": "agent_run_generation_unavailable",
            "level": "info",
            "run_id": str(run.id),
            "log_policy": "research",
            **expected_exception,
        }
    ]


@pytest.mark.asyncio
async def test_unwrapped_agent_response_invalid_error_marks_internal_error(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """runnerが包まずに出した応答の契約違反は分類の漏れとして、内部エラーで終える。"""
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    fake_agent = FakeAgent(
        exc=AgentOutputInvalidError(AgentOutputDefect.OUTPUT_SCHEMA_MISMATCH)
    )
    _patch_worker_execution(monkeypatch, lambda **_kwargs: fake_agent)

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )

    async with session_factory() as session:
        persisted = await session.get(AgentRun, run.id)
    assert persisted is not None
    assert (persisted.status, persisted.error_code) == ("failed", "internal_error")


@pytest.mark.asyncio
async def test_run_agent_answer_generation_error_preserves_death_progress_stage(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    fake_agent = FakeAgent(
        exc=AnsweringRunError(
            cause=AIProviderResultError(
                "SHOULD_NOT_LEAK", reason=AIProviderResultReason.RESPONSE_UNPARSEABLE
            )
        ),
        stage="evidence_collection",
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
async def test_run_agent_answer_unexpected_error_marks_internal_error(
    read_logs,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    fake_agent = FakeAgent(exc=RuntimeError("unexpected failure"))
    FakeLiveStreamPublisher.instances = []
    _patch_worker_execution(monkeypatch, lambda **_kwargs: fake_agent)
    monkeypatch.setattr(
        run_execution,
        "AgentRunLiveStreamPublisher",
        FakeLiveStreamPublisher,
    )

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )
    logs = read_logs()

    assert [
        entry for entry in logs if entry.get("event") == "agent_run_unexpected_error"
    ] == [
        {
            "event": "agent_run_unexpected_error",
            "level": "error",
            "run_id": str(run.id),
            "log_policy": "research",
            "error_class": "builtins.RuntimeError",
            "error_message": "unexpected failure",
        }
    ]

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


def test_run_agent_answer_declares_taskiq_timeout_without_application_timeout() -> None:
    task = agent_run_tasks.run_agent_answer

    assert task.task_name == "run_agent_answer"
    assert task.labels == {
        "timeout": agent_run_tasks.RESEARCH_TASKIQ_TIMEOUT_SECONDS,
        "max_retries": 0,
        "retry_on_error": False,
    }
    assert agent_run_tasks.RESEARCH_TASKIQ_TIMEOUT_SECONDS == 180
    assert not hasattr(agent_run_tasks, "RESEARCH_APPLICATION_TIMEOUT_SECONDS")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_start_commit_failure_leaves_queued_run_for_deadline_recovery(
    read_logs,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """開始のcommitが秘匿値つきのDB例外で落ちても、taskは例外を出さずログに秘匿値を残さず、Runと利用枠はqueuedのまま期限切れの回収に任せる。"""
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
        run_execution,
        "AgentRunLiveStreamPublisher",
        FakeLiveStreamPublisher,
    )

    try:
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
    logs = read_logs()

    assert_no_sensitive_marker(logs)
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


@pytest.mark.asyncio
async def test_unrecorded_failure_leaves_running_run_for_deadline_recovery(
    read_logs,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工程の失敗を記録するcommitが秘匿値つきのDB例外で落ちても、taskは例外を出さずログに秘匿値を残さず、Runはrunningのまま期限切れの回収に任せる。"""
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(
            session, question="SECRET_QUESTION_MARKER"
        )
        run_id = run.id
    injected = False

    def fail_failure_commit(session: Session) -> None:
        nonlocal injected
        if injected:
            return
        status = session.connection().scalar(
            select(AgentRun.status).where(AgentRun.id == run_id)
        )
        if status == "failed":
            injected = True
            raise SensitivePersistenceFailure

    _patch_worker_execution(
        monkeypatch,
        lambda **_kwargs: FakeAgent(),
        answering_runner=FakeAnsweringRunner(
            exc=AnsweringRunError(cause=PlanningError(cause=TimeoutError()))
        ),
    )
    FakeLiveStreamPublisher.instances = []
    monkeypatch.setattr(
        run_execution, "AgentRunLiveStreamPublisher", FakeLiveStreamPublisher
    )
    sa_event.listen(Session, "before_commit", fail_failure_commit)
    try:
        await agent_run_tasks.run_agent_answer(
            trigger=AgentRunTrigger(run_id=run_id), ctx=_ctx(session_factory)
        )
    finally:
        sa_event.remove(Session, "before_commit", fail_failure_commit)
    logs = read_logs()

    assert injected
    assert_no_sensitive_marker(logs)
    async with session_factory() as session:
        persisted = await session.get(AgentRun, run_id)
    assert persisted is not None
    assert (persisted.status, persisted.error_code) == ("running", None)
    assert [
        event
        for event in FakeLiveStreamPublisher.instances[0].published
        if isinstance(event, AgentRunLiveStreamTerminalEvent)
    ] == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_provider_timeout_error_follows_existing_unexpected_error_path(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    FakeLiveStreamPublisher.instances = []
    _patch_worker_execution(
        monkeypatch,
        lambda **_kwargs: FakeAgent(exc=TimeoutError("provider timeout")),
    )
    monkeypatch.setattr(
        run_execution,
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


@pytest.mark.asyncio
async def test_completion_rolls_back_written_artifacts(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """完了のcommitに失敗したRunは回答・出典・threadの変更を残さず、内部エラーで失敗する。"""
    async with session_factory() as session:
        thread, message, run = await _create_thread_message_run(session)
        original_handoff = {"existing": "handoff"}
        thread.research_handoff = original_handoff
        await session.commit()
        run_id, thread_id, message_id = run.id, thread.id, message.id
        original_updated_at = thread.updated_at

    injected = False

    def fail_completed_commit(session: Session) -> None:
        nonlocal injected
        if injected:
            return
        status = session.connection().scalar(
            select(AgentRun.status).where(AgentRun.id == run_id)
        )
        if status == "completed":
            injected = True
            raise RuntimeError("completion commit failed")

    fake_agent = FakeAgent(_external_result())
    _patch_worker_execution(
        monkeypatch,
        lambda **_kwargs: fake_agent,
        answering_runner=FakeAnsweringRunner(research_handoff=_handoff()),
    )
    FakeLiveStreamPublisher.instances = []
    monkeypatch.setattr(
        run_execution, "AgentRunLiveStreamPublisher", FakeLiveStreamPublisher
    )
    sa_event.listen(Session, "before_commit", fail_completed_commit)
    try:
        await agent_run_tasks.run_agent_answer(
            trigger=AgentRunTrigger(run_id=run_id), ctx=_ctx(session_factory)
        )
    finally:
        sa_event.remove(Session, "before_commit", fail_completed_commit)

    assert injected
    async with session_factory() as session:
        persisted = await session.get(AgentRun, run_id)
        saved_thread = await session.get(AgentThread, thread_id)
        assert persisted is not None and saved_thread is not None
        assert persisted.status == "failed"
        assert persisted.error_code == "internal_error"
        assert persisted.attempt_epoch == 1
        assert persisted.assistant_message_id is None
        assert saved_thread.research_handoff == original_handoff
        assert saved_thread.updated_at == original_updated_at
        assert list(
            await session.scalars(
                select(AgentMessage.id).where(AgentMessage.thread_id == thread_id)
            )
        ) == [message_id]
        assert list(await session.scalars(select(AgentMessageSource))) == []
    terminal = [
        event
        for event in FakeLiveStreamPublisher.instances[0].published
        if isinstance(event, AgentRunLiveStreamTerminalEvent)
    ]
    assert terminal == [
        AgentRunLiveStreamTerminalEvent(status="failed", errorCode="internal_error")
    ]


@pytest.mark.asyncio
async def test_start_after_deadline_notifies_redelivered_attempt_of_deadline(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """受理期限後に再配信されたrunningのRunは、元の世代のストリームへ期限切れを通知して終わる。"""
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(
            session,
            status="running",
            attempt_epoch=3,
            deadline_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    monkeypatch.setattr(FakeLiveStreamPublisher, "instances", [])
    monkeypatch.setattr(
        run_execution, "AgentRunLiveStreamPublisher", FakeLiveStreamPublisher
    )
    monkeypatch.setattr(run_execution, "build_answering_runner", ForbiddenConstruction)

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )

    async with session_factory() as session:
        persisted = await session.get(AgentRun, run.id)
        assert persisted is not None
    assert (
        [
            (publisher.attempt_epoch, publisher.published)
            for publisher in FakeLiveStreamPublisher.instances
        ],
        (persisted.status, persisted.attempt_epoch),
    ) == (
        [(3, [AgentRunLiveStreamTerminalEvent(status="deadline_exceeded")])],
        ("deadline_exceeded", 3),
    )


@pytest.mark.asyncio
async def test_question_read_failure_notifies_failed_terminal(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """開始後に質問を読めなかったRunは、internal_errorで失敗し、その世代へ失敗を通知する。"""
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)

    async def fail_to_read_question(*_args: object) -> None:
        raise RuntimeError("question read failed")

    monkeypatch.setattr(run_execution, "_read_user_question", fail_to_read_question)
    monkeypatch.setattr(FakeLiveStreamPublisher, "instances", [])
    monkeypatch.setattr(
        run_execution, "AgentRunLiveStreamPublisher", FakeLiveStreamPublisher
    )
    monkeypatch.setattr(run_execution, "build_answering_runner", ForbiddenConstruction)

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )

    async with session_factory() as session:
        persisted = await session.get(AgentRun, run.id)
        assert persisted is not None
    assert (
        [
            (publisher.attempt_epoch, publisher.published)
            for publisher in FakeLiveStreamPublisher.instances
        ],
        (persisted.status, persisted.error_code),
    ) == (
        [
            (
                1,
                [
                    AgentRunLiveStreamTerminalEvent(
                        status="failed", errorCode="internal_error"
                    )
                ],
            )
        ],
        ("failed", "internal_error"),
    )


class _DeadlinePassingAnsweringRunner:
    """回答を始める前に受理期限が過ぎた状態を作ってから、生成の失敗を送出する。"""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], run_id: UUID
    ) -> None:
        self._session_factory = session_factory
        self._run_id = run_id

    async def run(self, input: RunInput, *, identity: RunIdentity) -> RunResult:
        async with self._session_factory() as session:
            await session.execute(
                update(AgentRun)
                .where(AgentRun.id == self._run_id)
                .values(deadline_at=func.clock_timestamp() - timedelta(seconds=1))
            )
            await session.commit()
        raise AnsweringRunError(
            cause=AIProviderResponseError(
                reason=AIProviderResponseReason.SERVER_ERROR,
                http_error=HttpResponseError(status_code=503, received_at=_RECEIVED_AT),
            )
        )


@pytest.mark.asyncio
async def test_failure_after_deadline_notifies_deadline_instead_of_failure(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回答前に期限を過ぎてから生成に失敗したRunは、失敗ではなく期限切れで終わり、そのとおり通知する。"""
    async with session_factory() as session:
        _thread, _message, run = await _create_thread_message_run(session)
    monkeypatch.setattr(FakeLiveStreamPublisher, "instances", [])
    monkeypatch.setattr(
        run_execution, "AgentRunLiveStreamPublisher", FakeLiveStreamPublisher
    )
    monkeypatch.setattr(
        run_execution,
        "build_answering_runner",
        lambda **_kwargs: _DeadlinePassingAnsweringRunner(session_factory, run.id),
    )

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=run.id),
        ctx=_ctx(session_factory),
    )

    async with session_factory() as session:
        persisted = await session.get(AgentRun, run.id)
        assert persisted is not None
    terminal = [
        event
        for event in FakeLiveStreamPublisher.instances[0].published
        if isinstance(event, AgentRunLiveStreamTerminalEvent)
    ]
    assert (terminal, (persisted.status, persisted.error_code)) == (
        [AgentRunLiveStreamTerminalEvent(status="deadline_exceeded")],
        ("deadline_exceeded", None),
    )
