"""ResearchHandoffInput.from_run() の投影契約。

整理へ渡してよい範囲をこの型が決める。記事本文・URL・内部IDが漏れないこと、
「情報が無かった」と「検索できなかった」が区別できることを検証する。
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.agent.contract import ResearchHandoff, ResearchRunRecord, ResearchTaskRecord
from app.agent.evidence_collection.contract import (
    CollectedNews,
    CollectedTask,
    ResearchTaskReport,
)
from app.agent.evidence_collection.external_search.contract import ExternalSearchHit
from app.agent.evidence_review.answer_evidence import (
    AnswerEvidence,
    EvidenceRunCompleted,
    EvidenceRunFailed,
    ExternalSearchEvidence,
)
from app.agent.research_handoff import ResearchHandoffInput

_AS_OF = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)


def _handoff() -> ResearchHandoff:
    return ResearchHandoff(
        updated_at=_AS_OF,
        runs=(
            ResearchRunRecord(
                as_of=_AS_OF,
                tasks=(
                    ResearchTaskRecord(research_goal="goal", executed_queries=("q",)),
                ),
            ),
        ),
    )


def _collected(
    *,
    task_index: int = 0,
    research_goal: str = "goal",
    executed_queries: tuple[str, ...] = ("q",),
    external_collection: str = "succeeded",
    external_hits: list[ExternalSearchHit] | None = None,
) -> CollectedTask:
    hits = external_hits if external_hits is not None else []
    return CollectedTask(
        task_index=task_index,
        research_goal=research_goal,
        internal_hits=[],
        external_hits=hits,
        executed_queries=executed_queries,
        report=ResearchTaskReport(
            task_index=task_index,
            research_goal=research_goal,
            internal_collection="succeeded",
            external_collection=external_collection,  # type: ignore[arg-type]
            generated_queries=list(executed_queries),
            provider_failed_query_count=(
                len(executed_queries) if external_collection == "provider_failed" else 0
            ),
            external_hit_count=len(hits),
        ),
    )


def _hit(url: str, *, title: str) -> ExternalSearchHit:
    return ExternalSearchHit(url=url, title=title, content="本文" * 500)


def _completed(*evidence: ExternalSearchEvidence) -> EvidenceRunCompleted:
    return EvidenceRunCompleted(
        answer_evidence=AnswerEvidence(external_evidence=evidence),
        review_missing=("在庫水準",),
    )


def _evidence(*, task_index: int, claim: str, why: str) -> ExternalSearchEvidence:
    return ExternalSearchEvidence(
        option_index=task_index,
        task_index=task_index,
        claim=claim,
        why_selected=why,
        url="https://example.com/a",
        title="title",
    )


def _from_run(
    *,
    collected_news: CollectedNews,
    evidence_run: EvidenceRunCompleted | EvidenceRunFailed,
) -> ResearchHandoffInput:
    return ResearchHandoffInput.from_run(
        handoff=_handoff(),
        question="NVIDIAの供給は？",
        collected_news=collected_news,
        evidence_run=evidence_run,
        as_of=_AS_OF,
    )


def test_headlines_carry_titles_without_article_bodies_or_urls() -> None:
    """整理は記事本文を読まない。見出しだけを渡し、本文とURLは投影しない。"""
    hits = [_hit("https://example.com/a", title="供給網の記事")]
    collected = CollectedNews(tasks=[_collected(external_hits=hits)])

    result = _from_run(collected_news=collected, evidence_run=_completed())

    assert result.tasks[0].hit_headlines == ("供給網の記事",)


def test_headlines_include_articles_that_were_not_adopted() -> None:
    """何が手に入る状態だったかは、採用結果だけからは読めない。"""
    hits = [
        _hit("https://example.com/a", title="採用された記事"),
        _hit("https://example.com/b", title="採用されなかった記事"),
    ]
    collected = CollectedNews(tasks=[_collected(external_hits=hits)])
    evidence_run = _completed(_evidence(task_index=0, claim="claim", why="why"))

    result = _from_run(collected_news=collected, evidence_run=evidence_run)

    assert result.tasks[0].hit_headlines == ("採用された記事", "採用されなかった記事")


def test_adopted_pairs_each_claim_with_the_reason_it_was_selected() -> None:
    """整理は「どう選んだか」を読むため、claimと選定理由を対で渡す。"""
    collected = CollectedNews(tasks=[_collected()])
    evidence_run = _completed(
        _evidence(task_index=0, claim="供給は逼迫", why="一次情報のため")
    )

    result = _from_run(collected_news=collected, evidence_run=evidence_run)

    assert result.tasks[0].adopted == (("供給は逼迫", "一次情報のため"),)


def test_a_failed_search_is_distinguishable_from_one_that_found_nothing() -> None:
    """再検索の価値が真逆なため、外部収集の結末をそのまま渡す。"""
    collected = CollectedNews(
        tasks=[
            _collected(task_index=0, external_collection="succeeded"),
            _collected(
                task_index=1,
                research_goal="goal-b",
                external_collection="provider_failed",
            ),
        ]
    )

    result = _from_run(collected_news=collected, evidence_run=_completed())

    assert [task.external_collection for task in result.tasks] == [
        "succeeded",
        "provider_failed",
    ]


def test_a_failed_review_still_projects_what_was_searched() -> None:
    """精査が失敗しても、何を叩いて何が集まったかは整理へ渡す。"""
    hits = [_hit("https://example.com/a", title="集まった記事")]
    collected = CollectedNews(tasks=[_collected(external_hits=hits)])

    result = _from_run(
        collected_news=collected,
        evidence_run=EvidenceRunFailed(failure_reason="reviewer_failed"),
    )

    assert result.tasks[0].hit_headlines == ("集まった記事",)
    assert result.tasks[0].adopted == ()
    assert result.review_missing == ()
