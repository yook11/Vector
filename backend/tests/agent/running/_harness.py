"""Run全体(`AnsweringRunner.run()` 黒箱)テストが共有するharness。

AnsweringRunnerを組んで実行するという同一契約のfake協力者とデータbuilderだけを
置く。テスト専用の仕掛け(gate・blocking fake・probe・failure注入)は共有せず、
各テストファイルのそばに残す。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.direct_answer.contract import DirectAnswerDraft
from app.agent.answering.evidence_answer.contract import (
    EvidenceAnswerDraft,
    EvidenceAnswerOutcome,
    EvidenceAnswerUnavailable,
)
from app.agent.evidence_collection import CollectedNews
from app.agent.evidence_collection.external_search.contract import (
    ExternalQueryDraft,
    ExternalResearchRuntime,
    ExternalSearchCandidate,
)
from app.agent.evidence_collection.internal_search.contract import (
    InternalArticleContent,
    InternalArticleSearchHit,
)
from app.agent.evidence_review.draft import EvidenceReviewDraft
from app.agent.evidence_review.result import EvidenceRunResult
from app.agent.planning.contract import (
    ExternalResearchTask,
    SearchPlan,
    TargetTimeWindow,
)
from app.agent.question_context import QuestionContext
from app.agent.running import AnsweringRunner, RunContext, RunInput
from app.agent.running import answering_runner as answering_runner_module
from app.analysis.analyzed_article import InScopeAnalyzedArticle
from app.analysis.assessment.domain.result import InScope, InScopeCategory

RUN_ID = UUID("019bd239-1ed4-7fbb-a336-04fe3c197652")
AS_OF = datetime(2026, 7, 20, 9, 30, tzinfo=UTC)
DEFAULT_TARGET_TIME_WINDOW = TargetTimeWindow(kind="last_n_days", days=1)


def review_draft(
    selections: list[dict[str, Any]] | None = None,
    *,
    missing: list[str] | None = None,
) -> EvidenceReviewDraft:
    return EvidenceReviewDraft.model_validate(
        {"selections": selections or [], "missing": missing or []}
    )


def query_draft(queries: object) -> ExternalQueryDraft:
    # 非list入力の負パスも通せるようmodel_validate経由で組む。
    return ExternalQueryDraft.model_validate({"queries": queries})


def external_task(goal: str) -> ExternalResearchTask:
    return ExternalResearchTask(research_goal=goal)


def external_candidate(
    url: str, *, title: str | None = None
) -> ExternalSearchCandidate:
    return ExternalSearchCandidate(
        url=url,
        title=title or url.rsplit("/", maxsplit=1)[-1],
        snippet="snippet",
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


def external_research_runtime(
    *,
    query_runtime: object,
    reviewer_runtime: object,
    tool: object,
) -> ExternalResearchRuntime:
    return ExternalResearchRuntime(
        query_runtime=query_runtime,  # type: ignore[arg-type]
        reviewer_runtime=reviewer_runtime,  # type: ignore[arg-type]
        search_tool=tool,  # type: ignore[arg-type]
    )


class Preparer:
    def __init__(self, *, answer_requirements: tuple[str, ...] | None = None) -> None:
        self._answer_requirements = answer_requirements or ()

    async def prepare(self, **_kwargs: object) -> QuestionContext:
        return QuestionContext(
            standalone_question="NVIDIA の見通しは？",
            answer_requirements=self._answer_requirements,
        )


class Planner:
    def __init__(self, plan: SearchPlan) -> None:
        self._plan = plan

    async def plan(self, request: object) -> SearchPlan:
        del request
        return self._plan


class UnreachableDirectAnswerer:
    async def answer(
        self, *, request: AnsweringRequest, previous_answer: str = ""
    ) -> DirectAnswerDraft:
        raise AssertionError(
            f"direct answer must not run: {request!r} {previous_answer!r}"
        )


class EvidenceAnswerer:
    def __init__(self) -> None:
        self.calls: list[list[Any]] = []

    async def answer(
        self,
        *,
        request: AnsweringRequest,
        evidence: list[Any],
        target_time_window: TargetTimeWindow | None,
        review_missing: tuple[str, ...] = (),
    ) -> EvidenceAnswerOutcome:
        del request, target_time_window, review_missing
        self.calls.append(list(evidence))
        if evidence:
            return EvidenceAnswerDraft(
                answer="根拠に基づく回答です。",
                cited_refs=[item.source.source_ref for item in evidence],
            )
        # 自己申告でinsufficientを名乗る形は無くなったため、
        # evidenceが無く回答を作れなかった場合はunavailableで表す。
        return EvidenceAnswerUnavailable(failure_code="fake_no_evidence")


class Events:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def event_occurred(self, event: Any) -> None:
        self.events.append(event)


class ExternalSearchTool:
    def __init__(
        self, results_by_query: dict[str, list[ExternalSearchCandidate]] | None = None
    ) -> None:
        self._results = results_by_query or {}
        self.calls: list[Any] = []

    @property
    def name(self) -> str:
        return "external_search"

    async def invoke(self, input: Any) -> list[ExternalSearchCandidate]:
        self.calls.append(input)
        return list(self._results.get(input.query, []))


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


async def execute_run(runner: AnsweringRunner, *, as_of: datetime = AS_OF) -> Any:
    return await runner.run(
        RunInput(question="NVIDIA の見通しは？", history=()),
        run_context=RunContext(run_id=RUN_ID, as_of=as_of),
    )
