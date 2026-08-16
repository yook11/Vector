"""Evidence Review テストが共有する入力builder。

収集結果(CollectedTask)とReviewerへの投影(EvidenceReviewInput)を、
複数のテストモジュールから同じ形で組むために置く。
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.agent.evidence_collection.contract import CollectedTask, ResearchTaskReport
from app.agent.evidence_collection.external_search.contract import (
    ExternalSearchHit,
)
from app.agent.evidence_collection.internal_search.contract import (
    InternalArticleContent,
    InternalArticleSearchHit,
)
from app.agent.evidence_review.preparation import (
    EvidenceOption,
    EvidenceReviewInput,
    EvidenceReviewTaskGroup,
)
from app.analysis.analyzed_article import InScopeAnalyzedArticle
from app.analysis.assessment.domain.result import InScope, InScopeCategory

__all__ = [
    "AS_OF",
    "collected_task",
    "evidence_option",
    "external_hit",
    "internal_hit",
    "review_input",
    "sample_task",
    "task_group",
]

AS_OF = datetime(2026, 7, 20, 9, 30, tzinfo=UTC)


def collected_task(
    *,
    task_index: int,
    research_goal: str = "goal",
    internal_hits: list[InternalArticleSearchHit] | None = None,
    external_hits: list[ExternalSearchHit] | None = None,
) -> CollectedTask:
    internal = internal_hits or []
    external = external_hits or []
    # report は精査が読まない収集診断のため、成功形の最小値で埋める。
    return CollectedTask(
        task_index=task_index,
        research_goal=research_goal,
        internal_hits=internal,
        external_hits=external,
        executed_queries=(),
        report=ResearchTaskReport(
            task_index=task_index,
            research_goal=research_goal,
            internal_collection="succeeded",
            external_collection="succeeded",
            internal_hit_count=len(internal),
            external_hit_count=len(external),
        ),
    )


def internal_hit(
    *,
    assessment_id: int,
    curation_id: int,
    title: str,
    summary: str,
    key_points: list[str] | None = None,
    published_at: datetime | None = None,
) -> InternalArticleSearchHit:
    article = InScopeAnalyzedArticle(
        curation_id=curation_id,
        title=title,
        summary=summary,
        assessment_result=InScope(
            category=InScopeCategory.AI,
            investor_take="投資家視点",
            key_points=[
                {"content": point, "mentions": []} for point in key_points or []
            ],
        ),
    )
    return InternalArticleSearchHit(
        assessment_id=assessment_id,
        article=article,
        content=InternalArticleContent.from_article(article, published_at=published_at),
        distance=0.1,
    )


def external_hit(url: str, *, title: str | None = None) -> ExternalSearchHit:
    return ExternalSearchHit(
        url=url,
        title=title or url.rsplit("/", maxsplit=1)[-1],
        snippet="external snippet",
        source_name="Example",
        published_at=AS_OF,
    )


def sample_task(
    *, task_index: int, internal_count: int = 0, external_count: int = 0
) -> CollectedTask:
    """サンプルのtaskを1件用意する。各ヒットは出典として互いに区別できる値を持つ。"""
    return collected_task(
        task_index=task_index,
        internal_hits=[
            internal_hit(
                assessment_id=1000 + task_index * 100 + position,
                curation_id=task_index * 100 + position + 1,
                title=f"task{task_index}-internal-{position}",
                summary="s",
            )
            for position in range(internal_count)
        ],
        external_hits=[
            external_hit(f"https://example.com/task{task_index}/{position}")
            for position in range(external_count)
        ],
    )


def evidence_option(
    *,
    index: int,
    title: str,
    source_name: str | None = None,
    published_at: datetime | None = None,
    snippet: str | None = None,
) -> EvidenceOption:
    return EvidenceOption(
        index=index,
        title=title,
        source_name=source_name,
        published_at=published_at,
        snippet=snippet,
    )


def task_group(
    *,
    task_index: int = 0,
    goal: str = "NVIDIA の最新動向を確認する",
    options: tuple[EvidenceOption, ...] = (),
) -> EvidenceReviewTaskGroup:
    return EvidenceReviewTaskGroup(
        task_index=task_index,
        research_goal=goal,
        options=options,
    )


def review_input(
    *,
    goal: str = "NVIDIA の最新動向を確認する",
    options: tuple[EvidenceOption, ...] = (),
    as_of: datetime | None = None,
    task_groups: tuple[EvidenceReviewTaskGroup, ...] | None = None,
) -> EvidenceReviewInput:
    """単一groupのEvidenceReviewInputを組む。

    選択肢の渡し方はRun全体のtask_groups(通しindex空間)のため、
    goal/optionsは1個のEvidenceReviewTaskGroupへラップする。
    """
    return EvidenceReviewInput(
        task_groups=task_groups or (task_group(goal=goal, options=options),),
        as_of=as_of or AS_OF,
    )
