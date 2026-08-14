"""Evidence Reviewer のドメイン純関数契約(S1: Run単位のグループ化と復元)。

`build_review_task_groups`はRun内の全taskの候補をtask_index昇順のグループ列
(indexはRun全体の通し番号)へ組み、`build_review_evidence`はその通しindexから
所属taskと候補を復元しつつ範囲外/重複/上限超過をdropする。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import app.agent.evidence_review.contract as evidence_review_contract_module
import app.agent.evidence_review.policy as evidence_review_policy_module
from app.agent.evidence_collection.contract import CollectedTask, ResearchTaskReport
from app.agent.evidence_collection.external_search.contract import (
    CANDIDATE_SNIPPET_MAX_CHARS,
    ExternalSearchCandidate,
)
from app.agent.evidence_collection.internal_search.contract import (
    InternalArticleContent,
    InternalArticleSearchHit,
)
from app.agent.evidence_review.contract import (
    EVIDENCE_REVIEW_ADOPTION_LIMIT,
    EVIDENCE_REVIEW_MISSING_LIMIT,
    EvidenceReviewDraft,
    EvidenceReviewResult,
)
from app.agent.evidence_review.policy import (
    EVIDENCE_REVIEW_TIMEOUT_SECONDS,
    REVIEWER_ERROR_REASON,
    REVIEWER_TIMEOUT_REASON,
    EvidenceReviewPreparation,
    build_review_evidence,
    build_review_task_groups,
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


def _review_result(selections: list[dict[str, Any]]) -> EvidenceReviewResult:
    return EvidenceReviewResult.from_raw(selections=selections, missing=[])


def test_policy_exports_review_timeout_and_failure_reason_constants() -> None:
    """selector 一式の timeout/reason 定数がここへ改名移設されている。"""
    assert (
        {"resolve_reviewer_failure_reason"} <= set(dir(evidence_review_policy_module)),
        EVIDENCE_REVIEW_TIMEOUT_SECONDS,
        REVIEWER_TIMEOUT_REASON,
        REVIEWER_ERROR_REASON,
    ) == (True, 30, "reviewer_timeout", "reviewer_error")


def test_adoption_and_missing_caps_are_run_scoped_values() -> None:
    """S2(選別結果の復元)。cap の値がRun単位の15/8になる。

    task単位5件×3 taskの実質上限と同じ15を採用上限に、missing上限は
    それより絞った8にする(仕様「選別結果の復元」)。
    """
    assert (EVIDENCE_REVIEW_ADOPTION_LIMIT, EVIDENCE_REVIEW_MISSING_LIMIT) == (15, 8)
    assert not hasattr(
        evidence_review_contract_module, "EVIDENCE_REVIEW_ADOPTION_LIMIT_PER_TASK"
    )
    assert not hasattr(
        evidence_review_contract_module, "EVIDENCE_REVIEW_MISSING_LIMIT_PER_TASK"
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
    build_task_groups = build_review_task_groups
    tasks = [
        _collected_task(task_index=1, research_goal="goal-B"),
        _collected_task(task_index=0, research_goal="goal-A"),
    ]

    groups = build_task_groups(tasks)

    assert [group.task_index for group in groups] == [0, 1]
    assert [group.research_goal for group in groups] == ["goal-A", "goal-B"]


def test_task_groups_place_internal_candidates_before_external_within_a_group() -> None:
    """各グループ内は内部候補が先、外部候補が後。"""
    build_task_groups = build_review_task_groups
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
    build_task_groups = build_review_task_groups
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
    build_task_groups = build_review_task_groups
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
    build_task_groups = build_review_task_groups
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
    build_task_groups = build_review_task_groups

    assert build_task_groups([]) == ()


# --- build_review_evidence ---------------------------------------------------


def test_build_evidence_drops_reviewer_selections_of_nonexistent_candidates() -> None:
    """reviewerが実在しない候補を指した選択は、evidenceへ組み立てない。

    実在する候補への選択の組み立ては妨げない。実在の判定はRun全体の通しindex空間
    (全task合計の候補数)で行う。
    """
    build_evidence = build_review_evidence
    tasks = [
        _task_with_candidates(task_index=0, internal=1),
        _task_with_candidates(task_index=1, external=2),
    ]
    # 通しindex: 0=task0内部, 1..2=task1外部。実在の上限は全task合計の3。
    # index 2はtask0だけで数えると範囲外(候補1件)だが、Run全体では実在する。
    result = _review_result(
        [
            {"candidate_index": 2, "claim": "adopted", "why_selected": "why"},
            {"candidate_index": 3, "claim": "nonexistent", "why_selected": "why"},
        ],
    )

    internal_evidence, external_evidence, _dropped = build_evidence(
        tasks=tasks, selection_result=result
    )

    assert (
        [item.claim for item in internal_evidence],
        [item.claim for item in external_evidence],
    ) == ([], ["adopted"])


def test_build_evidence_drops_duplicate_index_keeping_the_first_selection() -> None:
    """同一の通しindexへの重複採用は最初の1件だけが残る。

    内部候補の重複は下流(curation_id重複排除)にも網があるが、外部候補の
    重複を防ぐ防波堤はここだけのため、外部候補で検証する。
    """
    build_evidence = build_review_evidence
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
    result = _review_result(
        [
            {"candidate_index": 1, "claim": "first-1", "why_selected": "why"},
            {"candidate_index": 1, "claim": "second-1", "why_selected": "why"},
            {"candidate_index": 0, "claim": "internal-claim", "why_selected": "why"},
        ]
    )

    internal_evidence, external_evidence, dropped = build_evidence(
        tasks=tasks, selection_result=result
    )

    assert (
        [item.claim for item in internal_evidence],
        [item.claim for item in external_evidence],
        dropped,
    ) == (["internal-claim"], ["first-1"], 1)


def test_build_evidence_caps_adoption_at_the_run_wide_limit_in_selection_order() -> (
    None
):
    """採用上限(現行値15、Run全体で共有)超過は採用順の後ろから切られる。"""
    build_evidence = build_review_evidence
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
    # 統合index空間: 0-7が内部、8-16が外部の合計17候補を全て有効なまま採用する。
    selections = [
        {"candidate_index": index, "claim": f"claim-{index}", "why_selected": "why"}
        for index in range(17)
    ]
    result = _review_result(selections)

    internal_evidence, external_evidence, dropped = build_evidence(
        tasks=tasks, selection_result=result
    )

    adopted_claims = [item.claim for item in internal_evidence] + [
        item.claim for item in external_evidence
    ]
    assert (adopted_claims, dropped) == (
        [f"claim-{index}" for index in range(EVIDENCE_REVIEW_ADOPTION_LIMIT)],
        len(selections) - EVIDENCE_REVIEW_ADOPTION_LIMIT,
    )


def test_build_evidence_accounts_for_every_selection_as_adopted_or_dropped() -> None:
    """採用数とdropped件数の合計が、入力された選択の総数と一致する(選択は黙って消えない)。

    drop理由(実在しない/重複/上限超過)のどれでも数えられるよう、入力に3理由を全て含める。
    """
    build_evidence = build_review_evidence
    tasks = [
        _task_with_candidates(task_index=0, external=EVIDENCE_REVIEW_ADOPTION_LIMIT + 1)
    ]
    # 有効な選択を上限+1件(最後の1件が上限超過)、重複1件、実在しない1件。
    selections = [
        {"candidate_index": index, "claim": f"claim-{index}", "why_selected": "why"}
        for index in range(EVIDENCE_REVIEW_ADOPTION_LIMIT + 1)
    ]
    selections.insert(
        1, {"candidate_index": 0, "claim": "duplicate", "why_selected": "why"}
    )
    selections.append(
        {
            "candidate_index": EVIDENCE_REVIEW_ADOPTION_LIMIT + 1,
            "claim": "nonexistent",
            "why_selected": "why",
        }
    )
    result = _review_result(selections)

    internal_evidence, external_evidence, dropped = build_evidence(
        tasks=tasks, selection_result=result
    )

    adopted_count = len(internal_evidence) + len(external_evidence)
    assert (adopted_count, dropped) == (
        EVIDENCE_REVIEW_ADOPTION_LIMIT,
        len(selections) - EVIDENCE_REVIEW_ADOPTION_LIMIT,
    )


def test_build_evidence_caps_selections_shared_across_multiple_tasks() -> None:
    """採用上限(現行値15)がRun全体で共有され、単一taskの上限ではない

    (2 task合計16候補を全採用しようとすると1件だけdropされる)。
    """
    build_evidence = build_review_evidence
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
    result = _review_result(selections)

    internal_evidence, external_evidence, dropped = build_evidence(
        tasks=tasks, selection_result=result
    )

    assert (
        [item.claim for item in internal_evidence],
        external_evidence,
        dropped,
    ) == (
        [f"claim-{index}" for index in range(EVIDENCE_REVIEW_ADOPTION_LIMIT)],
        [],
        len(selections) - EVIDENCE_REVIEW_ADOPTION_LIMIT,
    )


def test_build_evidence_restores_original_candidates_from_run_wide_indexes() -> None:
    """選択されたRun全体の通しindexから、元候補とその所属task・出典参照を復元する。

    回答に必要なevidenceの組み立てはmaps_inputs_to_*_evidence_fieldsの2本が持つ。
    source_refはf"{task_index}-{index}"の形の出典参照になる。
    """
    build_evidence = build_review_evidence
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
    build_evidence = build_review_evidence
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
    build_evidence = build_review_evidence
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


def test_build_evidence_maps_inputs_to_internal_evidence_fields() -> None:
    """内部候補・選択結果・task情報から、InternalArticleEvidenceの各fieldを正しく組み立てる。"""
    build_evidence = build_review_evidence
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


def test_build_evidence_maps_inputs_to_external_evidence_fields() -> None:
    """外部候補・選択結果・task情報から、ExternalSearchEvidenceの各fieldを正しく組み立てる。"""
    build_evidence = build_review_evidence
    candidate = ExternalSearchCandidate(
        url=SafeUrl("https://example.com/external-story"),
        title="external title",
        snippet="external snippet",
        source_name="Example News",
        published_at=_AS_OF,
    )
    tasks = [_collected_task(task_index=1, external_candidates=[candidate])]
    result = _review_result(
        [{"candidate_index": 0, "claim": "見出しの主張", "why_selected": "選定理由"}],
    )

    internal_evidence, external_evidence, dropped = build_evidence(
        tasks=tasks, selection_result=result
    )

    assert dropped == 0
    assert internal_evidence == []
    item = external_evidence[0]
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
    assert result.missing == [f"missing-{index}" for index in range(8)]
