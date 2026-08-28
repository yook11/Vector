"""ResearchHandoffInput.from_run() の組み立て契約。

整理へ渡してよい範囲をこの型が決める。記事本文・URL・内部IDが漏れないこと、
台帳の追記規則、作れないRunではNoneになることを検証する。
"""

from __future__ import annotations

from datetime import UTC, datetime

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
from app.agent.research_handoff.handoff import (
    ResearchHandoff,
    ResearchRunRecord,
    ResearchTaskRecord,
)

_AS_OF = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)
_QUESTION = "NVIDIAの供給は？"


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
    previous: ResearchHandoff | None = None,
    as_of: datetime = _AS_OF,
) -> ResearchHandoffInput | None:
    return ResearchHandoffInput.from_run(
        previous=previous,
        question=_QUESTION,
        collected_news=collected_news,
        evidence_run=evidence_run,
        as_of=as_of,
    )


def _assembled(
    *,
    collected_news: CollectedNews,
    evidence_run: EvidenceRunCompleted | None = None,
    previous: ResearchHandoff | None = None,
    as_of: datetime = _AS_OF,
) -> ResearchHandoffInput:
    result = _from_run(
        collected_news=collected_news,
        evidence_run=evidence_run if evidence_run is not None else _completed(),
        previous=previous,
        as_of=as_of,
    )
    assert result is not None
    return result


def test_headlines_carry_titles_without_article_bodies_or_urls() -> None:
    """整理は記事本文を読まない。見出しだけを渡し、本文とURLは投影しない。"""
    hits = [_hit("https://example.com/a", title="供給網の記事")]
    collected = CollectedNews(tasks=[_collected(external_hits=hits)])

    result = _assembled(collected_news=collected)

    assert result.tasks[0].hit_headlines == ("供給網の記事",)


def test_headlines_include_articles_that_were_not_adopted() -> None:
    """何が手に入る状態だったかは、採用結果だけからは読めない。"""
    hits = [
        _hit("https://example.com/a", title="採用された記事"),
        _hit("https://example.com/b", title="採用されなかった記事"),
    ]
    collected = CollectedNews(tasks=[_collected(external_hits=hits)])
    evidence_run = _completed(_evidence(task_index=0, claim="claim", why="why"))

    result = _assembled(collected_news=collected, evidence_run=evidence_run)

    assert result.tasks[0].hit_headlines == ("採用された記事", "採用されなかった記事")


def test_adopted_pairs_each_claim_with_the_reason_it_was_selected() -> None:
    """整理は「どう選んだか」を読むため、claimと選定理由を対で渡す。"""
    collected = CollectedNews(tasks=[_collected()])
    evidence_run = _completed(
        _evidence(task_index=0, claim="供給は逼迫", why="一次情報のため")
    )

    result = _assembled(collected_news=collected, evidence_run=evidence_run)

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

    result = _assembled(collected_news=collected)

    assert [task.external_collection for task in result.tasks] == [
        "succeeded",
        "provider_failed",
    ]


def test_a_failed_review_does_not_assemble_an_input() -> None:
    """精査に失敗したRunは申し送りを触らない。"""
    hits = [_hit("https://example.com/a", title="集まった記事")]
    collected = CollectedNews(tasks=[_collected(external_hits=hits)])

    result = _from_run(
        collected_news=collected,
        evidence_run=EvidenceRunFailed(failure_reason="reviewer_failed"),
    )

    assert result is None


def test_tasks_are_recorded_in_task_index_order_with_verbatim_queries() -> None:
    """次の調査はquery文字列そのものを読むため、加工せず順序ごと残す。"""
    collected = CollectedNews(
        tasks=[
            _collected(
                task_index=1,
                research_goal="goal-B",
                executed_queries=("q-b1", "q-b2"),
            ),
            _collected(
                task_index=0,
                research_goal="goal-A",
                executed_queries=("q-a1",),
            ),
        ]
    )

    result = _assembled(collected_news=collected)

    assert [
        (task.research_goal, task.executed_queries)
        for task in result.handoff.runs[0].tasks
    ] == [
        ("goal-A", ("q-a1",)),
        ("goal-B", ("q-b1", "q-b2")),
    ]


def test_task_with_empty_executed_queries_is_not_recorded() -> None:
    """外部検索へ到達しなかったtaskは、叩いたqueryが無いので台帳に残さない。"""
    collected = CollectedNews(
        tasks=[
            _collected(
                task_index=0,
                research_goal="goal-A",
                executed_queries=(),
            ),
            _collected(
                task_index=1,
                research_goal="goal-B",
                executed_queries=("q-b1",),
            ),
        ]
    )

    result = _assembled(collected_news=collected)

    assert [task.research_goal for task in result.handoff.runs[0].tasks] == ["goal-B"]


def test_zero_recordable_tasks_returns_none() -> None:
    """1本もqueryを叩けなかったRunは申し送りを触らない。"""
    collected = CollectedNews(
        tasks=[_collected(research_goal="goal-A", executed_queries=())]
    )

    assert _from_run(collected_news=collected, evidence_run=_completed()) is None


def test_as_of_is_carried_through_unchanged() -> None:
    """調査時点は鮮度の再確認かどうかの判断に使うため、丸めずに残す。"""
    as_of = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    collected = CollectedNews(tasks=[_collected(research_goal="goal-A")])

    result = _assembled(collected_news=collected, as_of=as_of)

    assert result.handoff.runs[0].as_of == as_of


def test_first_record_starts_a_handoff_with_nothing_organized_yet() -> None:
    """最初のRunでは整理する対象が無く、整理3本は空のまま台帳だけが立つ。"""
    collected = CollectedNews(tasks=[_collected(research_goal="goal-1")])

    result = _assembled(collected_news=collected)

    assert (
        [task.research_goal for task in result.handoff.runs[0].tasks],
        result.handoff.updated_at,
        result.handoff.collected_overview,
        result.handoff.unresolved_points,
        result.handoff.next_search_guidance,
    ) == (["goal-1"], _AS_OF, "", "", "")


def test_later_records_are_appended_in_execution_order_without_dropping() -> None:
    """上限が無いため、古い記録は Run を重ねても落ちない。"""
    handoff: ResearchHandoff | None = None
    for day in (1, 2, 3, 4):
        as_of = datetime(2026, 8, day, tzinfo=UTC)
        result = _assembled(
            collected_news=CollectedNews(
                tasks=[_collected(research_goal=f"goal-{day}")]
            ),
            previous=handoff,
            as_of=as_of,
        )
        handoff = result.handoff

    assert handoff is not None
    assert (
        [run.tasks[0].research_goal for run in handoff.runs],
        handoff.updated_at,
    ) == (
        ["goal-1", "goal-2", "goal-3", "goal-4"],
        datetime(2026, 8, 4, tzinfo=UTC),
    )


def test_appending_carries_the_previous_organized_text_forward() -> None:
    """台帳を積む時点では整理を書き直さない。整理工程が失敗しても前回値が残る。"""
    previous = ResearchHandoff(
        updated_at=datetime(2026, 8, 1, tzinfo=UTC),
        runs=(
            ResearchRunRecord(
                as_of=datetime(2026, 8, 1, tzinfo=UTC),
                tasks=(
                    ResearchTaskRecord(
                        research_goal="goal-1",
                        executed_queries=("q",),
                    ),
                ),
            ),
        ),
        collected_overview="Blackwell の供給記事が集まっている",
        unresolved_points="在庫水準は確認できていない",
        next_search_guidance="一次情報を優先する",
    )

    result = _assembled(
        collected_news=CollectedNews(tasks=[_collected(research_goal="goal-2")]),
        previous=previous,
    )

    assert (
        result.handoff.collected_overview,
        result.handoff.unresolved_points,
        result.handoff.next_search_guidance,
    ) == (
        previous.collected_overview,
        previous.unresolved_points,
        previous.next_search_guidance,
    )
