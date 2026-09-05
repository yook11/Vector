"""Run全体(`AnsweringRunner.run()` 黒箱)テストが共有するharness。

AnsweringRunnerを組んで実行するという同一契約のfake協力者とデータbuilderだけを
置く。必須portの成功fakeは共有し、blocking・failure注入は各テストのそばに残す。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from app.agent.answering.direct_answer.contract import (
    DirectAnswerDraft,
    DirectAnswerInput,
)
from app.agent.answering.evidence_answer.contract import (
    EvidenceAnswerDraft,
    EvidenceAnswerInput,
)
from app.agent.evidence_collection import CollectedNews
from app.agent.evidence_collection.external_search import ExternalSearchService
from app.agent.evidence_collection.external_search.contract import (
    ExternalQueryDraft,
    ExternalSearch,
    ExternalSearchHit,
)
from app.agent.evidence_collection.internal_search.contract import (
    InternalArticleContent,
    InternalArticleSearchHit,
)
from app.agent.evidence_review.answer_evidence import EvidenceRunResult
from app.agent.evidence_review.selection import EvidenceReviewerDraft
from app.agent.planning.contract import (
    ExternalResearchTask,
    SearchPlan,
    TargetTimeWindow,
)
from app.agent.research_handoff import ResearchHandoff, ResearchHandoffInput
from app.agent.running import AnsweringRunner, RunIdentity, RunInput
from app.agent.running import answering_runner as answering_runner_module
from app.agent.runs.execution import Continue, Stop
from app.analysis.analyzed_article import InScopeAnalyzedArticle
from app.analysis.assessment.domain.result import InScope, InScopeCategory

RUN_ID = UUID("019bd239-1ed4-7fbb-a336-04fe3c197652")
USER_ID = UUID("019bd239-1ed4-7fbb-a336-04fe3c197650")
THREAD_ID = UUID("019bd239-1ed4-7fbb-a336-04fe3c197651")
AS_OF = datetime(2026, 7, 20, 9, 30, tzinfo=UTC)
DEFAULT_TARGET_TIME_WINDOW = TargetTimeWindow(kind="last_n_days", days=1)


class AllowAnswerGenerationStart:
    def __init__(self, timeline: list[str] | None = None) -> None:
        self._timeline = timeline
        self.calls = 0
        self.start_calls = 0
        self.check_calls = 0
        self.authorize_calls = 0

    async def start_answer_generation(self) -> Continue:
        self.calls += 1
        self.start_calls += 1
        if self._timeline is not None:
            self._timeline.append("answer_start")
        return Continue()

    async def authorize_answer_regeneration(self) -> Continue:
        self.authorize_calls += 1
        return Continue()

    async def check_answer_generation_continuation(self) -> Continue:
        self.check_calls += 1
        return Continue()


class ScriptedAnswerGenerationRepository:
    def __init__(
        self,
        *,
        start: Continue | Stop | BaseException = Continue(),
        checks: list[Continue | Stop] | None = None,
        authorizes: list[Continue | Stop | BaseException] | None = None,
        timeline: list[str] | None = None,
    ) -> None:
        self.calls = 0
        self.start_calls = 0
        self.check_calls = 0
        self.authorize_calls = 0
        self._start = start
        self._checks = list(checks or [])
        self._authorizes = list(authorizes or [])
        self._timeline = timeline

    async def start_answer_generation(self) -> Continue | Stop:
        self.calls += 1
        self.start_calls += 1
        if self._timeline is not None:
            self._timeline.append("answer_start")
        if isinstance(self._start, BaseException):
            raise self._start
        return self._start

    async def authorize_answer_regeneration(self) -> Continue | Stop:
        self.authorize_calls += 1
        if not self._authorizes:
            return Continue()
        outcome = self._authorizes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def check_answer_generation_continuation(self) -> Continue | Stop:
        self.check_calls += 1
        if not self._checks:
            return Continue()
        return self._checks.pop(0)


def review_draft(
    selections: list[dict[str, Any]] | None = None,
    *,
    missing: list[str] | None = None,
) -> EvidenceReviewerDraft:
    return EvidenceReviewerDraft.model_validate(
        {"selections": selections or [], "missing": missing or []}
    )


def query_draft(queries: object) -> ExternalQueryDraft:
    # 非list入力の負パスも通せるようmodel_validate経由で組む。
    return ExternalQueryDraft.model_validate({"queries": queries})


def external_task(goal: str) -> ExternalResearchTask:
    return ExternalResearchTask(research_goal=goal)


def external_hit(url: str, *, title: str | None = None) -> ExternalSearchHit:
    return ExternalSearchHit(
        url=url,
        title=title or url.rsplit("/", maxsplit=1)[-1],
        content="content",
        source_name="Example",
        published_at=AS_OF,
    )


def internal_hit(
    *,
    assessment_id: int,
    curation_id: int,
    title: str,
    summary: str | None = None,
) -> InternalArticleSearchHit:
    article = InScopeAnalyzedArticle(
        curation_id=curation_id,
        title=title,
        summary=summary or f"{title} summary",
        assessment_result=InScope(
            category=InScopeCategory.AI,
            investor_take="投資家視点",
            key_points=[],
        ),
    )
    return InternalArticleSearchHit(
        assessment_id=assessment_id,
        article=article,
        content=InternalArticleContent.from_article(article, published_at=None),
        distance=0.1,
    )


def fixed_scope[T](value: T) -> Callable[[], AbstractAsyncContextManager[T]]:
    """同じ値を貸し続けるscope factory。資源のlifecycle検証は各テスト側で行う。"""

    @asynccontextmanager
    async def scope() -> AsyncIterator[T]:
        yield value

    return scope


@dataclass(frozen=True, slots=True)
class ExternalScopes:
    """1 Run分の外部資源。collectorとreviewerがそれぞれ別に借りる。"""

    external_search: ExternalSearch
    reviewer_runtime: object


def external_research_runtime(
    *,
    query_runtime: object,
    reviewer_runtime: object,
    gateway: object,
) -> ExternalScopes:
    return ExternalScopes(
        external_search=ExternalSearchService(
            query_runtime=query_runtime,  # type: ignore[arg-type]
            search_gateway=gateway,  # type: ignore[arg-type]
        ),
        reviewer_runtime=reviewer_runtime,
    )


class Planner:
    def __init__(self, plan: SearchPlan) -> None:
        self._plan = plan

    async def plan(self, input: object) -> SearchPlan:
        del input
        return self._plan


class UnreachableDirectAnswerer:
    async def answer(self, input: DirectAnswerInput) -> DirectAnswerDraft:
        raise AssertionError(f"direct answer must not run: {input!r}")


class PassThroughOrganizer:
    """整理をせずhandoffをそのまま返す。台帳の配線だけを見るテスト用。"""

    def __init__(self) -> None:
        self.inputs: list[ResearchHandoffInput] = []

    async def organize(self, input: ResearchHandoffInput) -> ResearchHandoff:
        self.inputs.append(input)
        return input.handoff


class EvidenceAnswerer:
    def __init__(self) -> None:
        self.calls: list[list[Any]] = []

    async def answer(self, input: EvidenceAnswerInput) -> EvidenceAnswerDraft:
        evidence = list(input.evidence)
        self.calls.append(evidence)
        if evidence:
            return EvidenceAnswerDraft(
                answer="根拠に基づく回答です。",
                cited_refs=[item.source.source_ref for item in evidence],
            )
        return EvidenceAnswerDraft(
            answer="確認できる根拠がありませんでした。", cited_refs=[]
        )


class Events:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def event_occurred(self, event: Any) -> None:
        self.events.append(event)


class FakeExternalSearchGateway:
    def __init__(
        self, results_by_query: dict[str, list[ExternalSearchHit]] | None = None
    ) -> None:
        self._results = results_by_query or {}
        self.calls: list[Any] = []

    async def search(self, request: Any) -> list[ExternalSearchHit]:
        self.calls.append(request)
        return list(self._results.get(request.query, []))


@dataclass(frozen=True, slots=True)
class CapturedEvidenceAssemblyInput:
    """Answerer直前に分離された収集結果とEvidence Run結果。"""

    collected_news: CollectedNews
    evidence_run: EvidenceRunResult


def capture_external_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> list[CapturedEvidenceAssemblyInput]:
    """回答組み立てへ渡る収集結果とEvidence Run結果を記録する。"""
    captured: list[CapturedEvidenceAssemblyInput] = []
    original = answering_runner_module.assemble_evidence_result

    def capture(**kwargs: Any) -> Any:
        captured.append(
            CapturedEvidenceAssemblyInput(
                collected_news=kwargs["collected_news"],
                evidence_run=kwargs["evidence_run"],
            )
        )
        return original(**kwargs)

    monkeypatch.setattr(answering_runner_module, "assemble_evidence_result", capture)
    return captured


def run_identity(
    *,
    user_id: UUID = USER_ID,
    run_id: UUID = RUN_ID,
    thread_id: UUID = THREAD_ID,
    as_of: datetime = AS_OF,
) -> RunIdentity:
    return RunIdentity(
        user_id=user_id,
        run_id=run_id,
        thread_id=thread_id,
        as_of=as_of,
    )


async def execute_run(runner: AnsweringRunner, *, as_of: datetime = AS_OF) -> Any:
    return await runner.run(
        RunInput(question="NVIDIA の見通しは？", history=()),
        identity=run_identity(as_of=as_of),
    )
