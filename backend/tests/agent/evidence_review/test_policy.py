"""Evidence Reviewer のドメイン純関数契約(S1: Run単位の候補復元と回答根拠化)。

`EvidenceReviewPreparation`はRun内の候補をtask_index昇順のグループ列
(indexはRun全体の通し番号)へ組む。`AnswerEvidence.from_reviewer_response`は
Reviewerの選択をそのindexから復元し、回答に渡せる一意なEvidence集合へ確定する。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

import app.agent.evidence_review.contract as evidence_review_contract_module
import app.agent.evidence_review.policy as evidence_review_policy_module
from app.agent.evidence_collection.contract import CollectedTask, ResearchTaskReport
from app.agent.evidence_collection.external_search.contract import (
    CANDIDATE_SNIPPET_MAX_CHARS,
    ExternalSearchCandidate,
    ExternalSearchEvidence,
)
from app.agent.evidence_collection.internal_search.contract import (
    InternalArticleContent,
    InternalArticleSearchHit,
)
from app.agent.evidence_review.contract import (
    ANSWER_EVIDENCE_LIMIT,
    EVIDENCE_REVIEW_MISSING_LIMIT,
    EVIDENCE_REVIEWER_SELECTION_LIMIT,
    AnswerEvidence,
    EvidenceReviewDraft,
    EvidenceReviewerResponse,
    EvidenceReviewerSelection,
    EvidenceReviewOutcome,
    EvidenceReviewPreparation,
    InternalArticleEvidence,
)
from app.agent.evidence_review.policy import (
    EVIDENCE_REVIEW_TIMEOUT_SECONDS,
    REVIEWER_ERROR_REASON,
    REVIEWER_TIMEOUT_REASON,
    finalize_review_draft,
    resolve_reviewer_failure_reason,
)
from app.analysis.analyzed_article import InScopeAnalyzedArticle
from app.analysis.assessment.domain.result import InScope, InScopeCategory
from app.shared.security.safe_url import SafeUrl

_AS_OF = datetime(2026, 7, 20, 9, 30, tzinfo=UTC)


def _collected_task(
    *,
    task_index: int,
    research_goal: str = "goal",
    internal_hits: list[InternalArticleSearchHit] | None = None,
    external_candidates: list[ExternalSearchCandidate] | None = None,
) -> CollectedTask:
    hits = internal_hits or []
    candidates = external_candidates or []
    # report は policy が読まない収集診断のため、成功形の最小値で埋める。
    return CollectedTask(
        task_index=task_index,
        research_goal=research_goal,
        internal_hits=hits,
        external_candidates=candidates,
        executed_queries=(),
        report=ResearchTaskReport(
            task_index=task_index,
            research_goal=research_goal,
            internal_collection="succeeded",
            external_collection="succeeded",
            internal_candidate_count=len(hits),
            external_candidate_count=len(candidates),
        ),
    )


def _internal_hit(
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


def _external_candidate(
    url: str, *, title: str | None = None
) -> ExternalSearchCandidate:
    return ExternalSearchCandidate(
        url=url,
        title=title or url.rsplit("/", maxsplit=1)[-1],
        snippet="external snippet",
        source_name="Example",
        published_at=_AS_OF,
    )


def _task_with_candidates(
    *, task_index: int, internal: int = 0, external: int = 0
) -> CollectedTask:
    """indexの数勘定だけを扱うテスト用。候補の中身は勘定に影響しない捨て値で埋める。"""
    return _collected_task(
        task_index=task_index,
        internal_hits=[
            _internal_hit(
                assessment_id=1000 + task_index * 100 + position,
                curation_id=task_index * 100 + position + 1,
                title=f"task{task_index}-internal-{position}",
                summary="s",
            )
            for position in range(internal)
        ],
        external_candidates=[
            _external_candidate(f"https://example.com/task{task_index}/{position}")
            for position in range(external)
        ],
    )


def _reviewer_response(selections: list[dict[str, Any]]) -> EvidenceReviewerResponse:
    return EvidenceReviewerResponse.from_raw(selections=selections, missing=[])


def _internal_article_evidence(
    *, curation_id: int, source_ref: str
) -> InternalArticleEvidence:
    return InternalArticleEvidence(
        source_ref=source_ref,
        task_index=0,
        claim="claim",
        why_selected="why",
        assessment_id=1000 + curation_id,
        curation_id=curation_id,
        title=f"internal-{curation_id}",
        summary="summary",
        key_points=[],
        published_at=_AS_OF,
    )


def _external_evidence(*, url: str, source_ref: str) -> ExternalSearchEvidence:
    return ExternalSearchEvidence(
        source_ref=source_ref,
        task_index=0,
        claim="claim",
        why_selected="why",
        url=url,
        title="external",
        snippet="snippet",
        published_at=_AS_OF,
        source_name="Example",
    )


def test_policy_exports_review_timeout_and_failure_reason_constants() -> None:
    """selector 一式の timeout/reason 定数がここへ改名移設されている。"""
    assert (
        {"resolve_reviewer_failure_reason"} <= set(dir(evidence_review_policy_module)),
        EVIDENCE_REVIEW_TIMEOUT_SECONDS,
        REVIEWER_TIMEOUT_REASON,
        REVIEWER_ERROR_REASON,
    ) == (True, 30, "reviewer_timeout", "reviewer_error")


def test_policy_does_not_expose_answer_evidence_construction() -> None:
    """回答用Evidenceの構築はAnswerEvidence factoryだけが公開する。"""
    assert not hasattr(evidence_review_policy_module, "build_answer_evidence")


def test_answer_evidence_and_missing_caps_are_run_scoped_values() -> None:
    """回答用EvidenceとmissingのcapはRun単位の15/8になる。

    task単位5件×3 taskの実質上限と同じ15をAnswerer入力上限に、missing上限は
    それより絞った8にする。
    """
    assert (ANSWER_EVIDENCE_LIMIT, EVIDENCE_REVIEW_MISSING_LIMIT) == (15, 8)
    assert not hasattr(
        evidence_review_contract_module, "ANSWER_EVIDENCE_LIMIT_PER_TASK"
    )
    assert not hasattr(
        evidence_review_contract_module, "EVIDENCE_REVIEW_MISSING_LIMIT_PER_TASK"
    )


def test_reviewer_response_rejects_more_than_the_selection_limit() -> None:
    selections = [
        EvidenceReviewerSelection(
            candidate_index=index,
            claim=f"claim-{index}",
            why_selected="why",
        )
        for index in range(EVIDENCE_REVIEWER_SELECTION_LIMIT + 1)
    ]

    with pytest.raises(ValidationError):
        EvidenceReviewerResponse(selections=selections)


def test_failed_review_outcome_rejects_evidence_or_missing() -> None:
    with pytest.raises(ValueError):
        EvidenceReviewOutcome(
            answer_evidence=AnswerEvidence(
                external_sources=(
                    _external_evidence(
                        url="https://example.com/evidence",
                        source_ref="1",
                    ),
                )
            ),
            missing=[],
            failure_reason="reviewer_error",
        )

    with pytest.raises(ValueError):
        EvidenceReviewOutcome(
            answer_evidence=AnswerEvidence(),
            missing=["missing"],
            failure_reason="reviewer_error",
        )


def test_resolve_reviewer_failure_reason_prefers_reason_then_code_then_fallback() -> (
    None
):
    resolve_failure_reason = resolve_reviewer_failure_reason

    assert (
        resolve_failure_reason(reason="timeout", code="ai_error_network"),
        resolve_failure_reason(reason=None, code="ai_error_network"),
        resolve_failure_reason(reason=None, code=None),
    ) == ("timeout", "ai_error_network", "reviewer_error")


# --- EvidenceReviewPreparation(往復) ---------------------------------------


def test_preparation_resolves_shown_indexes_to_their_original_candidates() -> None:
    """投影で見せた全ての通しindexが、渡した元の候補そのものへ解決される(往復)。

    投影とentryは同一の採番で作られるため、見せた番号と引ける番号はズレない。
    """
    internal = [
        _internal_hit(assessment_id=1001, curation_id=1, title="A-int-1", summary="s"),
        _internal_hit(assessment_id=1002, curation_id=2, title="A-int-2", summary="s"),
    ]
    external = [
        _external_candidate("https://example.com/b1", title="B-ext-1"),
        _external_candidate("https://example.com/b2", title="B-ext-2"),
    ]
    tasks = [
        _collected_task(task_index=2, internal_hits=internal),
        _collected_task(task_index=5, external_candidates=external),
    ]

    preparation = EvidenceReviewPreparation.from_tasks(tasks)

    resolved = [
        preparation.resolve_candidate(candidate.index)
        for group in preparation.task_groups
        for candidate in group.candidates
    ]
    assert [(entry.source, entry.task_index) for entry in resolved if entry] == [
        (internal[0], 2),
        (internal[1], 2),
        (external[0], 5),
        (external[1], 5),
    ]


def test_preparation_does_not_resolve_an_index_that_was_never_shown() -> None:
    """投影に載せていない番号は解決できずNoneになる(見せた番号だけが引ける)。

    負の番号もtupleの末尾参照に化けさせず構造的に弾く。
    """
    tasks = [
        _task_with_candidates(task_index=0, internal=1),
        _task_with_candidates(task_index=1, external=2),
    ]

    preparation = EvidenceReviewPreparation.from_tasks(tasks)

    shown_indexes = [
        candidate.index
        for group in preparation.task_groups
        for candidate in group.candidates
    ]
    assert (
        preparation.resolve_candidate(max(shown_indexes) + 1),
        preparation.resolve_candidate(-1),
    ) == (None, None)


# --- build_review_task_groups -----------------------------------------------


def test_task_groups_are_ordered_by_task_index_regardless_of_input_order() -> None:
    """グループの並びがtask_index昇順である(入力順に依存しない)。"""
    from_tasks = EvidenceReviewPreparation.from_tasks
    tasks = [
        _collected_task(task_index=1, research_goal="goal-B"),
        _collected_task(task_index=0, research_goal="goal-A"),
    ]

    groups = from_tasks(tasks).task_groups

    assert [group.task_index for group in groups] == [0, 1]
    assert [group.research_goal for group in groups] == ["goal-A", "goal-B"]


def test_task_groups_place_internal_candidates_before_external_within_a_group() -> None:
    """各グループ内は内部候補が先、外部候補が後。"""
    from_tasks = EvidenceReviewPreparation.from_tasks
    tasks = [
        _collected_task(
            task_index=0,
            internal_hits=[
                _internal_hit(
                    assessment_id=1001,
                    curation_id=1,
                    title="internal-a",
                    summary="summary-a",
                ),
                _internal_hit(
                    assessment_id=1002,
                    curation_id=2,
                    title="internal-b",
                    summary="summary-b",
                ),
            ],
            external_candidates=[
                _external_candidate("https://example.com/x", title="external-x"),
                _external_candidate("https://example.com/y", title="external-y"),
            ],
        )
    ]

    groups = from_tasks(tasks).task_groups

    assert [
        (candidate.index, candidate.title) for candidate in groups[0].candidates
    ] == [
        (0, "internal-a"),
        (1, "internal-b"),
        (2, "external-x"),
        (3, "external-y"),
    ]


def test_task_groups_assign_a_run_wide_index_without_duplication_across_groups() -> (
    None
):
    """indexがRun全体の通し番号であり、グループをまたいで重複しない。"""
    from_tasks = EvidenceReviewPreparation.from_tasks
    tasks = [
        _collected_task(
            task_index=0,
            internal_hits=[
                _internal_hit(
                    assessment_id=1001, curation_id=1, title="A-int", summary="s"
                )
            ],
            external_candidates=[
                _external_candidate("https://example.com/a", title="A-ext")
            ],
        ),
        _collected_task(
            task_index=1,
            internal_hits=[
                _internal_hit(
                    assessment_id=1002, curation_id=2, title="B-int", summary="s"
                )
            ],
        ),
    ]

    groups = from_tasks(tasks).task_groups

    ordered = [
        (candidate.index, candidate.title)
        for group in groups
        for candidate in group.candidates
    ]
    assert ordered == [(0, "A-int"), (1, "A-ext"), (2, "B-int")]
    all_indexes = [
        candidate.index for group in groups for candidate in group.candidates
    ]
    assert len(all_indexes) == len(set(all_indexes))


def test_task_groups_keep_a_task_with_no_candidates_as_an_empty_group() -> None:
    """候補が内外ともゼロのtaskもグループとして残る(欠番を作らない)。"""
    from_tasks = EvidenceReviewPreparation.from_tasks
    tasks = [
        _collected_task(task_index=0, research_goal="goal-A"),
        _collected_task(
            task_index=1,
            research_goal="goal-B",
            internal_hits=[
                _internal_hit(
                    assessment_id=1001, curation_id=1, title="B-int", summary="s"
                )
            ],
        ),
        _collected_task(task_index=2, research_goal="goal-C"),
    ]

    groups = from_tasks(tasks).task_groups

    assert [group.task_index for group in groups] == [0, 1, 2]
    assert groups[0].candidates == ()
    assert [candidate.index for candidate in groups[1].candidates] == [0]
    assert groups[2].candidates == ()


def test_task_groups_map_internal_source_name_to_none_and_truncate_snippet() -> None:
    """内部候補: source_name=None、snippetはsummaryとkey_points連結をcapでtruncate。

    summary単体ではcapに届かない入力で、連結の実施と連結後のtruncateを区別する。
    """
    from_tasks = EvidenceReviewPreparation.from_tasks
    summary = "short summary"
    point = "p" * CANDIDATE_SNIPPET_MAX_CHARS
    hit = _internal_hit(
        assessment_id=1001,
        curation_id=1,
        title="internal",
        summary=summary,
        key_points=[point],
        published_at=_AS_OF,
    )
    tasks = [_collected_task(task_index=0, internal_hits=[hit])]

    groups = from_tasks(tasks).task_groups

    candidate = groups[0].candidates[0]
    # 連結形式(summaryの次行に"- "付きkey_point)は投影仕様として直書きする。
    expected_snippet = f"{summary}\n- {point}"[:CANDIDATE_SNIPPET_MAX_CHARS]
    assert candidate.source_name is None
    assert candidate.published_at == _AS_OF
    assert candidate.snippet == expected_snippet


def test_task_groups_is_empty_tuple_when_there_are_no_tasks() -> None:
    from_tasks = EvidenceReviewPreparation.from_tasks

    assert from_tasks([]).task_groups == ()


# --- AnswerEvidence.from_reviewer_response -------------------------------------


def test_answer_evidence_factory_excludes_nonexistent_reviewer_selections() -> None:
    """見せていない候補indexは回答用Evidenceへ復元しない。"""
    tasks = [
        _task_with_candidates(task_index=0, internal=1),
        _task_with_candidates(task_index=1, external=2),
    ]
    # 通しindex: 0=task0内部, 1..2=task1外部。実在の上限は全task合計の3。
    # index 2はtask0だけで数えると範囲外(候補1件)だが、Run全体では実在する。
    result = _reviewer_response(
        [
            {"candidate_index": 2, "claim": "adopted", "why_selected": "why"},
            {"candidate_index": 3, "claim": "nonexistent", "why_selected": "why"},
        ],
    )

    evidence = AnswerEvidence.from_reviewer_response(
        preparation=EvidenceReviewPreparation.from_tasks(tasks),
        reviewer_response=result,
    )

    assert (
        isinstance(evidence, AnswerEvidence),
        [item.claim for item in evidence.internal_articles],
        [item.claim for item in evidence.external_sources],
    ) == (True, [], ["adopted"])


def test_answer_evidence_factory_keeps_the_first_reviewer_selection_for_an_index() -> (
    None
):
    """同じ候補indexを複数回選んでも、先に選んだ1件だけを回答に渡す。"""
    tasks = [
        _collected_task(
            task_index=0,
            internal_hits=[
                _internal_hit(
                    assessment_id=1000, curation_id=1, title="internal-0", summary="s"
                )
            ],
            external_candidates=[
                _external_candidate("https://example.com/0"),
                _external_candidate("https://example.com/1"),
            ],
        )
    ]
    # 統合index空間: 0が内部、1-2が外部の合計3候補。index 1を重複採用する。
    result = _reviewer_response(
        [
            {"candidate_index": 1, "claim": "first-1", "why_selected": "why"},
            {"candidate_index": 1, "claim": "second-1", "why_selected": "why"},
            {"candidate_index": 0, "claim": "internal-claim", "why_selected": "why"},
        ]
    )

    evidence = AnswerEvidence.from_reviewer_response(
        preparation=EvidenceReviewPreparation.from_tasks(tasks),
        reviewer_response=result,
    )

    assert (
        [item.claim for item in evidence.internal_articles],
        [item.claim for item in evidence.external_sources],
    ) == (["internal-claim"], ["first-1"])


def test_answer_evidence_factory_applies_the_limit_after_source_deduplication() -> None:
    """重複sourceは枠を消費せず、Answererには先着の一意な15件を渡す。"""
    tasks = [
        _collected_task(
            task_index=0,
            external_candidates=[
                _external_candidate("https://example.com/duplicate"),
                _external_candidate("https://example.com/duplicate"),
                *[
                    _external_candidate(f"https://example.com/{index}")
                    for index in range(2, ANSWER_EVIDENCE_LIMIT + 2)
                ],
            ],
        )
    ]
    selections = [
        {"candidate_index": index, "claim": f"claim-{index}", "why_selected": "why"}
        for index in range(ANSWER_EVIDENCE_LIMIT + 2)
    ]

    unsafe_response = EvidenceReviewerResponse.model_construct(
        selections=[
            EvidenceReviewerSelection.model_validate(selection)
            for selection in selections
        ],
        missing=[],
    )
    evidence = AnswerEvidence.from_reviewer_response(
        preparation=EvidenceReviewPreparation.from_tasks(tasks),
        reviewer_response=unsafe_response,
    )

    assert [item.claim for item in evidence.external_sources] == [
        "claim-0",
        *[f"claim-{index}" for index in range(2, ANSWER_EVIDENCE_LIMIT + 1)],
    ]


def test_answer_evidence_rejects_more_than_the_answerer_input_limit() -> None:
    """AnswerEvidence自身もAnswerer入力上限を超える集合を受け付けない。"""
    with pytest.raises(ValidationError):
        AnswerEvidence(
            external_sources=[
                _external_evidence(
                    url=f"https://example.com/{index}", source_ref=f"0-{index}"
                )
                for index in range(ANSWER_EVIDENCE_LIMIT + 1)
            ]
        )


def test_answer_evidence_rejects_duplicate_internal_source_identity() -> None:
    """内部記事のsource identityであるcuration_idは集合内で一意に保つ。"""
    with pytest.raises(ValidationError):
        AnswerEvidence(
            internal_articles=[
                _internal_article_evidence(curation_id=1, source_ref="0-0"),
                _internal_article_evidence(curation_id=1, source_ref="0-1"),
            ]
        )


def test_answer_evidence_rejects_duplicate_external_source_identity() -> None:
    """外部記事のsource identityであるURLは集合内で一意に保つ。"""
    with pytest.raises(ValidationError):
        AnswerEvidence(
            external_sources=[
                _external_evidence(
                    url="https://example.com/duplicate", source_ref="0-0"
                ),
                _external_evidence(
                    url="https://example.com/duplicate", source_ref="0-1"
                ),
            ]
        )


def test_answer_evidence_rejects_duplicate_source_ref_across_source_types() -> None:
    """内外を問わずsource_refは回答内で一意に保つ。"""
    with pytest.raises(ValidationError):
        AnswerEvidence(
            internal_articles=[
                _internal_article_evidence(curation_id=1, source_ref="0-0")
            ],
            external_sources=[
                _external_evidence(url="https://example.com/external", source_ref="0-0")
            ],
        )


def test_answer_evidence_factory_deduplicates_source_identities_across_the_run() -> (
    None
):
    """同一sourceを複数taskで選んでも、内外とも選択順で先の1件だけを残す。"""
    tasks = [
        _collected_task(
            task_index=0,
            internal_hits=[
                _internal_hit(
                    assessment_id=1001,
                    curation_id=42,
                    title="task-0 internal",
                    summary="s",
                ),
            ],
            external_candidates=[_external_candidate("https://example.com/duplicate")],
        ),
        _collected_task(
            task_index=1,
            internal_hits=[
                _internal_hit(
                    assessment_id=1002,
                    curation_id=42,
                    title="task-1 internal",
                    summary="s",
                ),
            ],
            external_candidates=[_external_candidate("https://example.com/duplicate")],
        ),
    ]
    evidence = AnswerEvidence.from_reviewer_response(
        preparation=EvidenceReviewPreparation.from_tasks(tasks),
        reviewer_response=_reviewer_response(
            [
                {
                    "candidate_index": 3,
                    "claim": "external-first",
                    "why_selected": "why",
                },
                {
                    "candidate_index": 2,
                    "claim": "internal-first",
                    "why_selected": "why",
                },
                {
                    "candidate_index": 1,
                    "claim": "external-later",
                    "why_selected": "why",
                },
                {
                    "candidate_index": 0,
                    "claim": "internal-later",
                    "why_selected": "why",
                },
            ]
        ),
    )

    assert (
        [
            (item.claim, item.task_index, item.curation_id)
            for item in evidence.internal_articles
        ],
        [
            (item.claim, item.task_index, str(item.url))
            for item in evidence.external_sources
        ],
    ) == (
        [("internal-first", 1, 42)],
        [("external-first", 1, "https://example.com/duplicate")],
    )


def test_answer_evidence_factory_restores_original_candidates_from_indexes() -> None:
    """選択indexから、元候補のtask・出典情報を回答用Evidenceへ復元する。"""
    tasks = [
        _collected_task(
            task_index=2,
            internal_hits=[
                _internal_hit(
                    assessment_id=1001, curation_id=1, title="A-int-1", summary="s"
                ),
                _internal_hit(
                    assessment_id=1002, curation_id=2, title="A-int-2", summary="s"
                ),
            ],
        ),
        _collected_task(
            task_index=5,
            external_candidates=[
                _external_candidate("https://example.com/b1", title="B-ext-1"),
                _external_candidate("https://example.com/b2", title="B-ext-2"),
            ],
        ),
    ]
    result = _reviewer_response(
        [
            {"candidate_index": 1, "claim": "A-int-2 claim", "why_selected": "why"},
            {"candidate_index": 3, "claim": "B-ext-2 claim", "why_selected": "why"},
        ],
    )

    evidence = AnswerEvidence.from_reviewer_response(
        preparation=EvidenceReviewPreparation.from_tasks(tasks),
        reviewer_response=result,
    )

    assert [
        (item.title, item.task_index, item.source_ref)
        for item in evidence.internal_articles
    ] == [("A-int-2", 2, "2-1")]
    assert [
        (item.title, item.task_index, item.source_ref)
        for item in evidence.external_sources
    ] == [("B-ext-2", 5, "5-3")]


def test_answer_evidence_factory_resolves_indexes_in_task_index_order() -> None:
    """通しindexの割当ては入力順でなくtask_index昇順に従う

    (build_review_task_groupsが作る入力と対応させるため)。
    """
    tasks = [
        _collected_task(
            task_index=1,
            internal_hits=[
                _internal_hit(
                    assessment_id=1002, curation_id=2, title="B-int", summary="s"
                )
            ],
        ),
        _collected_task(
            task_index=0,
            internal_hits=[
                _internal_hit(
                    assessment_id=1001, curation_id=1, title="A-int", summary="s"
                )
            ],
        ),
    ]
    # task_index昇順(0, 1)で結合すればindex 0はA-int、1はB-intになるはず。
    result = _reviewer_response(
        [{"candidate_index": 0, "claim": "claim", "why_selected": "why"}],
    )

    evidence = AnswerEvidence.from_reviewer_response(
        preparation=EvidenceReviewPreparation.from_tasks(tasks),
        reviewer_response=result,
    )

    assert [(item.title, item.task_index) for item in evidence.internal_articles] == [
        ("A-int", 0)
    ]


def test_answer_evidence_factory_preserves_indexes_around_an_empty_task() -> None:
    """候補ゼロのtaskが混ざってもindexの対応がずれない。"""
    tasks = [
        _collected_task(
            task_index=0,
            internal_hits=[
                _internal_hit(
                    assessment_id=1001, curation_id=1, title="A-int", summary="s"
                )
            ],
        ),
        _collected_task(task_index=1),
        _collected_task(
            task_index=2,
            internal_hits=[
                _internal_hit(
                    assessment_id=1002, curation_id=2, title="C-int", summary="s"
                )
            ],
        ),
    ]
    result = _reviewer_response(
        [{"candidate_index": 1, "claim": "C-int claim", "why_selected": "why"}],
    )

    evidence = AnswerEvidence.from_reviewer_response(
        preparation=EvidenceReviewPreparation.from_tasks(tasks),
        reviewer_response=result,
    )

    assert (
        len(evidence.external_sources),
        [(item.title, item.task_index) for item in evidence.internal_articles],
    ) == (0, [("C-int", 2)])


def test_answer_evidence_factory_maps_inputs_to_internal_evidence_fields() -> None:
    """内部候補・選択結果・task情報から、回答用内部Evidenceの各fieldを復元する。"""
    hit = _internal_hit(
        assessment_id=2001,
        curation_id=42,
        title="internal title",
        summary="internal summary",
        key_points=["key point one"],
        published_at=_AS_OF,
    )
    tasks = [_collected_task(task_index=1, internal_hits=[hit])]
    result = _reviewer_response(
        [{"candidate_index": 0, "claim": "見出しの主張", "why_selected": "選定理由"}],
    )

    evidence = AnswerEvidence.from_reviewer_response(
        preparation=EvidenceReviewPreparation.from_tasks(tasks),
        reviewer_response=result,
    )

    assert len(evidence.external_sources) == 0
    item = evidence.internal_articles[0]
    assert item.claim == "見出しの主張"
    assert item.why_selected == "選定理由"
    assert item.assessment_id == 2001
    assert item.curation_id == 42
    assert item.title == "internal title"
    assert item.summary == "internal summary"
    assert item.key_points == ["key point one"]
    assert item.published_at == _AS_OF
    assert item.task_index == 1
    assert item.source_ref == "1-0"


def test_answer_evidence_factory_maps_inputs_to_external_evidence_fields() -> None:
    """外部候補・選択結果・task情報から、回答用外部Evidenceの各fieldを復元する。"""
    candidate = ExternalSearchCandidate(
        url=SafeUrl("https://example.com/external-story"),
        title="external title",
        snippet="external snippet",
        source_name="Example News",
        published_at=_AS_OF,
    )
    tasks = [_collected_task(task_index=1, external_candidates=[candidate])]
    result = _reviewer_response(
        [{"candidate_index": 0, "claim": "見出しの主張", "why_selected": "選定理由"}],
    )

    evidence = AnswerEvidence.from_reviewer_response(
        preparation=EvidenceReviewPreparation.from_tasks(tasks),
        reviewer_response=result,
    )

    assert len(evidence.internal_articles) == 0
    item = evidence.external_sources[0]
    assert item.claim == "見出しの主張"
    assert item.why_selected == "選定理由"
    assert str(item.url) == "https://example.com/external-story"
    assert item.title == "external title"
    assert item.snippet == "external snippet"
    assert item.source_name == "Example News"
    assert item.published_at == _AS_OF
    assert item.task_index == 1
    assert item.source_ref == "1-0"


def test_finalize_review_draft_clamps_values_to_existing_contract() -> None:
    draft = EvidenceReviewDraft.model_validate(
        {
            "selections": [
                {
                    "candidate_index": 0,
                    "claim": "c" * 350,
                    "why_selected": "w" * 350,
                }
            ],
            "missing": ["m" * 250],
        }
    )

    result = finalize_review_draft(draft)

    assert (
        len(result.selections[0].claim),
        len(result.selections[0].why_selected),
        len(result.missing[0]),
    ) == (300, 300, 200)


def test_finalize_review_draft_clamps_missing_item_count_to_the_run_wide_limit() -> (
    None
):
    """S2(選別結果の復元)。reviewerが9件以上のmissingを返しても

    Run単位のmissing上限(8)へclampされる。
    """
    draft = EvidenceReviewDraft.model_validate(
        {
            "selections": [],
            "missing": [f"missing-{index}" for index in range(9)],
        }
    )

    result = finalize_review_draft(draft)

    assert len(result.missing) == 8
    assert result.missing == tuple(f"missing-{index}" for index in range(8))
