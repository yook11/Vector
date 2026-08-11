"""Evidence Reviewer のドメイン純関数契約(S1: Run単位のグループ化と復元)。

`build_review_task_groups`はRun内の全taskの候補をtask_index昇順のグループ列
(indexはRun全体の通し番号)へ組み、`build_review_evidence`はその通しindexから
所属taskと候補を復元しつつ範囲外/重複/上限超過をdropする。
production 未実装のため getattr ガードで参照する。
"""

from __future__ import annotations

from datetime import UTC, datetime
from importlib import import_module
from types import ModuleType
from typing import Any

import pytest

from app.agent.evidence_collection.contract import CollectedTask, ResearchTaskReport
from app.agent.evidence_collection.external_search.contract import (
    CANDIDATE_SNIPPET_MAX_CHARS,
    ExternalSearchCandidate,
)
from app.agent.evidence_collection.internal_search.contract import (
    InternalArticleContent,
    InternalArticleSearchHit,
)
from app.analysis.analyzed_article import InScopeAnalyzedArticle
from app.analysis.assessment.domain.result import InScope, InScopeCategory

_AS_OF = datetime(2026, 7, 20, 9, 30, tzinfo=UTC)


def _policy() -> ModuleType:
    try:
        return import_module("app.agent.evidence_review.policy")
    except ModuleNotFoundError as exc:
        pytest.fail(
            "evidence_review のドメイン純関数は policy module に置く必要が"
            f"あります ({exc.name})",
            pytrace=False,
        )


def _function(name: str) -> Any:
    value = getattr(_policy(), name, None)
    if value is None:
        pytest.fail(f"evidence_review policy must export {name}", pytrace=False)
    return value


def _contracts() -> ModuleType:
    return import_module("app.agent.evidence_review.contract")


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


def _review_result(policy: ModuleType, selections: list[dict[str, Any]]) -> Any:
    del policy
    result_type = getattr(_contracts(), "EvidenceReviewResult", None)
    if result_type is None:
        pytest.fail("evidence_review contract must export EvidenceReviewResult")
    return result_type.from_raw(selections=selections, missing=[])


def test_policy_exports_review_timeout_and_failure_reason_constants() -> None:
    """selector 一式の timeout/reason 定数がここへ改名移設されている。"""
    policy = _policy()

    assert (
        {"resolve_reviewer_failure_reason"} <= set(dir(policy)),
        policy.EVIDENCE_REVIEW_TIMEOUT_SECONDS,
        policy.REVIEWER_TIMEOUT_REASON,
        policy.REVIEWER_ERROR_REASON,
    ) == (True, 30, "reviewer_timeout", "reviewer_error")


def test_adoption_and_missing_caps_are_run_scoped_values() -> None:
    """S2(選別結果の復元)。cap の値がRun単位の15/8になる。

    task単位5件×3 taskの実質上限と同じ15を採用上限に、missing上限は
    それより絞った8にする(仕様「選別結果の復元」)。
    """
    contracts = _contracts()

    adoption_limit = getattr(contracts, "EVIDENCE_REVIEW_ADOPTION_LIMIT", None)
    missing_limit = getattr(contracts, "EVIDENCE_REVIEW_MISSING_LIMIT", None)
    if adoption_limit is None or missing_limit is None:
        pytest.fail(
            "evidence_review contract must export "
            "EVIDENCE_REVIEW_ADOPTION_LIMIT and EVIDENCE_REVIEW_MISSING_LIMIT "
            "(task単位を表さないRun単位の名前)"
        )

    assert (adoption_limit, missing_limit) == (15, 8)
    assert not hasattr(contracts, "EVIDENCE_REVIEW_ADOPTION_LIMIT_PER_TASK")
    assert not hasattr(contracts, "EVIDENCE_REVIEW_MISSING_LIMIT_PER_TASK")


def test_resolve_reviewer_failure_reason_prefers_reason_then_code_then_fallback() -> (
    None
):
    resolve_failure_reason = _function("resolve_reviewer_failure_reason")

    assert (
        resolve_failure_reason(reason="timeout", code="ai_error_network"),
        resolve_failure_reason(reason=None, code="ai_error_network"),
        resolve_failure_reason(reason=None, code=None),
    ) == ("timeout", "ai_error_network", "reviewer_error")


# --- build_review_task_groups -----------------------------------------------


def test_task_groups_are_ordered_by_task_index_regardless_of_input_order() -> None:
    """グループの並びがtask_index昇順である(入力順に依存しない)。"""
    build_task_groups = _function("build_review_task_groups")
    tasks = [
        _collected_task(task_index=1, research_goal="goal-B"),
        _collected_task(task_index=0, research_goal="goal-A"),
    ]

    groups = build_task_groups(tasks)

    assert [group.task_index for group in groups] == [0, 1]
    assert [group.research_goal for group in groups] == ["goal-A", "goal-B"]


def test_task_groups_place_internal_candidates_before_external_within_a_group() -> None:
    """各グループ内は内部候補が先、外部候補が後。"""
    build_task_groups = _function("build_review_task_groups")
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

    groups = build_task_groups(tasks)

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
    build_task_groups = _function("build_review_task_groups")
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

    groups = build_task_groups(tasks)

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
    build_task_groups = _function("build_review_task_groups")
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

    groups = build_task_groups(tasks)

    assert [group.task_index for group in groups] == [0, 1, 2]
    assert groups[0].candidates == ()
    assert [candidate.index for candidate in groups[1].candidates] == [0]
    assert groups[2].candidates == ()


def test_task_groups_map_internal_source_name_to_none_and_truncate_snippet() -> None:
    """内部候補: source_name=None、snippetはsummary+key_points連結をcapでtruncate。"""
    build_task_groups = _function("build_review_task_groups")
    overlong_summary = "s" * (CANDIDATE_SNIPPET_MAX_CHARS + 50)
    hit = _internal_hit(
        assessment_id=1001,
        curation_id=1,
        title="internal",
        summary=overlong_summary,
        key_points=["point-a"],
        published_at=_AS_OF,
    )
    tasks = [_collected_task(task_index=0, internal_hits=[hit])]

    groups = build_task_groups(tasks)

    candidate = groups[0].candidates[0]
    assert candidate.source_name is None
    assert candidate.published_at == _AS_OF
    assert len(candidate.snippet) == CANDIDATE_SNIPPET_MAX_CHARS
    assert candidate.snippet == overlong_summary[:CANDIDATE_SNIPPET_MAX_CHARS]


def test_task_groups_is_empty_tuple_when_there_are_no_tasks() -> None:
    build_task_groups = _function("build_review_task_groups")

    assert build_task_groups([]) == ()


# --- build_review_evidence ---------------------------------------------------


def test_build_evidence_drops_out_of_range_index_across_all_tasks() -> None:
    """統合index空間の範囲外(全taskの合計候補数以上)をdrop。"""
    build_evidence = _function("build_review_evidence")
    tasks = [
        _collected_task(
            task_index=0,
            internal_hits=[
                _internal_hit(
                    assessment_id=1001, curation_id=1, title="internal", summary="s"
                )
            ],
            external_candidates=[_external_candidate("https://example.com/only")],
        )
    ]
    result = _review_result(
        _policy(),
        [
            {"candidate_index": 0, "claim": "internal claim", "why_selected": "why"},
            {"candidate_index": 1, "claim": "external claim", "why_selected": "why"},
            {"candidate_index": 2, "claim": "out of range", "why_selected": "why"},
        ],
    )

    internal_evidence, external_evidence, dropped = build_evidence(
        tasks=tasks, selection_result=result
    )

    assert (
        [item.claim for item in internal_evidence],
        [item.claim for item in external_evidence],
        dropped,
    ) == (["internal claim"], ["external claim"], 1)


def test_build_evidence_drops_duplicate_index_and_caps_at_the_run_wide_limit() -> None:
    """重複indexと採用上限(現行値15、Run全体で共有)超過をdrop。"""
    build_evidence = _function("build_review_evidence")
    tasks = [
        _collected_task(
            task_index=0,
            internal_hits=[
                _internal_hit(
                    assessment_id=1000 + index,
                    curation_id=index + 1,
                    title=f"internal-{index}",
                    summary="s",
                )
                for index in range(8)
            ],
            external_candidates=[
                _external_candidate(f"https://example.com/{index}")
                for index in range(9)
            ],
        )
    ]
    # 統合index空間: 0-7が内部、8-16が外部の合計17候補。
    selections = [
        {"candidate_index": index, "claim": f"claim-{index}", "why_selected": "why"}
        for index in [0, 0, *range(1, 17)]
    ]
    result = _review_result(_policy(), selections)

    internal_evidence, external_evidence, dropped = build_evidence(
        tasks=tasks, selection_result=result
    )

    assert (
        len(internal_evidence) + len(external_evidence),
        dropped,
    ) == (15, 3)


def test_build_evidence_caps_selections_shared_across_multiple_tasks() -> None:
    """採用上限(現行値15)がRun全体で共有され、単一taskの上限ではない

    (2 task合計16候補を全採用しようとすると1件だけdropされる)。
    """
    build_evidence = _function("build_review_evidence")
    tasks = [
        _collected_task(
            task_index=0,
            internal_hits=[
                _internal_hit(
                    assessment_id=1000 + index,
                    curation_id=index + 1,
                    title=f"A-{index}",
                    summary="s",
                )
                for index in range(8)
            ],
        ),
        _collected_task(
            task_index=1,
            internal_hits=[
                _internal_hit(
                    assessment_id=1010 + index,
                    curation_id=10 + index,
                    title=f"B-{index}",
                    summary="s",
                )
                for index in range(8)
            ],
        ),
    ]
    selections = [
        {"candidate_index": index, "claim": f"claim-{index}", "why_selected": "why"}
        for index in range(16)
    ]
    result = _review_result(_policy(), selections)

    internal_evidence, external_evidence, dropped = build_evidence(
        tasks=tasks, selection_result=result
    )

    assert (len(internal_evidence) + len(external_evidence), dropped) == (15, 1)


def test_build_evidence_restores_task_and_candidate_from_a_cross_task_index() -> None:
    """通しindexから所属taskと候補が復元され、source_refがf"{task_index}-{index}"になる。"""
    build_evidence = _function("build_review_evidence")
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
    result = _review_result(
        _policy(),
        [
            {"candidate_index": 1, "claim": "A-int-2 claim", "why_selected": "why"},
            {"candidate_index": 3, "claim": "B-ext-2 claim", "why_selected": "why"},
        ],
    )

    internal_evidence, external_evidence, dropped = build_evidence(
        tasks=tasks, selection_result=result
    )

    assert dropped == 0
    assert [
        (item.title, item.task_index, item.source_ref) for item in internal_evidence
    ] == [("A-int-2", 2, "2-1")]
    assert [
        (item.title, item.task_index, item.source_ref) for item in external_evidence
    ] == [("B-ext-2", 5, "5-3")]


def test_build_evidence_uses_task_index_ascending_order_regardless_of_input_order() -> (
    None
):
    """通しindexの割当ては入力順でなくtask_index昇順に従う

    (build_review_task_groupsが作る入力と対応させるため)。
    """
    build_evidence = _function("build_review_evidence")
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
    result = _review_result(
        _policy(),
        [{"candidate_index": 0, "claim": "claim", "why_selected": "why"}],
    )

    internal_evidence, _external_evidence, dropped = build_evidence(
        tasks=tasks, selection_result=result
    )

    assert dropped == 0
    assert [(item.title, item.task_index) for item in internal_evidence] == [
        ("A-int", 0)
    ]


def test_build_evidence_keeps_index_alignment_when_a_task_has_no_candidates() -> None:
    """候補ゼロのtaskが混ざってもindexの対応がずれない。"""
    build_evidence = _function("build_review_evidence")
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
    result = _review_result(
        _policy(),
        [{"candidate_index": 1, "claim": "C-int claim", "why_selected": "why"}],
    )

    internal_evidence, external_evidence, dropped = build_evidence(
        tasks=tasks, selection_result=result
    )

    assert dropped == 0
    assert external_evidence == []
    assert [(item.title, item.task_index) for item in internal_evidence] == [
        ("C-int", 2)
    ]


def test_build_evidence_reconstructs_internal_provenance_and_keeps_claim() -> None:
    """保証するテスト条件 6。内部採用はclaimを持ち、既存provenanceを復元する。"""
    build_evidence = _function("build_review_evidence")
    hit = _internal_hit(
        assessment_id=2001,
        curation_id=42,
        title="internal title",
        summary="internal summary",
        key_points=["key point one"],
        published_at=_AS_OF,
    )
    tasks = [_collected_task(task_index=1, internal_hits=[hit])]
    result = _review_result(
        _policy(),
        [{"candidate_index": 0, "claim": "見出しの主張", "why_selected": "選定理由"}],
    )

    internal_evidence, external_evidence, dropped = build_evidence(
        tasks=tasks, selection_result=result
    )

    assert dropped == 0
    assert external_evidence == []
    item = internal_evidence[0]
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


def test_finalize_review_draft_clamps_values_to_existing_contract() -> None:
    contracts = _contracts()
    draft_type = getattr(contracts, "EvidenceReviewDraft", None)
    if draft_type is None:
        pytest.fail("evidence_review contract must export EvidenceReviewDraft")
    finalize_review_draft = _function("finalize_review_draft")
    draft = draft_type.model_validate(
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
    contracts = _contracts()
    draft_type = getattr(contracts, "EvidenceReviewDraft", None)
    if draft_type is None:
        pytest.fail("evidence_review contract must export EvidenceReviewDraft")
    finalize_review_draft = _function("finalize_review_draft")
    draft = draft_type.model_validate(
        {
            "selections": [],
            "missing": [f"missing-{index}" for index in range(9)],
        }
    )

    result = finalize_review_draft(draft)

    assert len(result.missing) == 8
    assert result.missing == [f"missing-{index}" for index in range(8)]
