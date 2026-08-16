"""回答用Evidenceと、Evidence Runの確定結果型の契約。

`AnswerEvidence.from_reviewer_response`はReviewerの選択を通しindexから復元し、
出典の重複を排して回答へ渡せる一意なEvidence集合に確定する。確定した結果は
`EvidenceRunCompleted` / `EvidenceRunFailed` としてRunの成否を型で表す。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agent.contract import ANSWER_EVIDENCE_LIMIT, EVIDENCE_REVIEW_MISSING_LIMIT
from app.agent.evidence_collection.external_search.contract import (
    MISSING_ITEM_MAX_CHARS,
    ExternalSearchHit,
)
from app.agent.evidence_review.answer_evidence import (
    AnswerEvidence,
    EvidenceRunCompleted,
    EvidenceRunFailed,
    ExternalSearchEvidence,
    InternalArticleEvidence,
)
from app.agent.evidence_review.preparation import EvidenceReviewPreparation
from app.agent.evidence_review.selection import EvidenceReviewerResponse
from app.shared.security.safe_url import SafeUrl
from tests.agent.evidence_review._builders import (
    AS_OF,
    collected_task,
    external_hit,
    internal_hit,
)


def _reviewer_response(selections: list[dict[str, object]]) -> EvidenceReviewerResponse:
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
        published_at=AS_OF,
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
        published_at=AS_OF,
        source_name="Example",
    )


# --- AnswerEvidence.from_reviewer_response -------------------------------------


def test_answer_evidence_factory_excludes_nonexistent_reviewer_selections() -> None:
    """見せていないoption_indexは回答用Evidenceへ復元しない。"""
    tasks = [
        collected_task(
            task_index=0,
            external_hits=[
                external_hit("https://example.com/shown-0"),
                external_hit("https://example.com/shown-1"),
            ],
        )
    ]
    # 見せた番号は0と1。2は列に無い。
    evidence = AnswerEvidence.from_reviewer_response(
        preparation=EvidenceReviewPreparation.from_tasks(tasks),
        reviewer_response=_reviewer_response(
            [
                {"option_index": 1, "claim": "shown", "why_selected": "why"},
                {"option_index": 2, "claim": "not-shown", "why_selected": "why"},
            ]
        ),
    )

    assert [(item.claim, str(item.url)) for item in evidence.external_sources] == [
        ("shown", "https://example.com/shown-1")
    ]


def test_answer_evidence_factory_keeps_the_first_reviewer_selection_for_an_index() -> (
    None
):
    """同じoption_indexを複数回選んでも、先に選んだ1件だけを回答に渡す。"""
    tasks = [
        collected_task(
            task_index=0,
            internal_hits=[
                internal_hit(
                    assessment_id=1000, curation_id=1, title="internal-0", summary="s"
                )
            ],
            external_hits=[
                external_hit("https://example.com/0"),
                external_hit("https://example.com/1"),
            ],
        )
    ]
    # 統合index空間: 0が内部、1-2が外部の合計3選択肢。index 1を重複採用する。
    result = _reviewer_response(
        [
            {"option_index": 1, "claim": "first-1", "why_selected": "why"},
            {"option_index": 1, "claim": "second-1", "why_selected": "why"},
            {"option_index": 0, "claim": "internal-claim", "why_selected": "why"},
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


def test_answer_evidence_rejects_duplicate_internal_source_identity_within_task() -> (
    None
):
    """同じtask内の内部検索の記事は集合内で一意に保つ。"""
    with pytest.raises(ValidationError):
        AnswerEvidence(
            internal_articles=[
                _internal_article_evidence(curation_id=1, source_ref="0-0"),
                _internal_article_evidence(curation_id=1, source_ref="0-1"),
            ]
        )


def test_answer_evidence_rejects_duplicate_external_source_identity_within_task() -> (
    None
):
    """同じtask内の外部記事URLは集合内で一意に保つ。"""
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


def test_factory_keeps_first_selection_for_duplicate_url_within_task() -> None:
    """同じtask内でURLが重複した場合は、Reviewerが最初に選んだ1件だけを残す。"""
    tasks = [
        collected_task(
            task_index=0,
            external_hits=[
                external_hit("https://example.com/duplicate"),
                external_hit("https://example.com/duplicate"),
            ],
        )
    ]
    evidence = AnswerEvidence.from_reviewer_response(
        preparation=EvidenceReviewPreparation.from_tasks(tasks),
        reviewer_response=_reviewer_response(
            [
                {
                    "option_index": 1,
                    "claim": "selected-first",
                    "why_selected": "why",
                },
                {
                    "option_index": 0,
                    "claim": "selected-later",
                    "why_selected": "why",
                },
            ]
        ),
    )

    assert [
        (item.task_index, item.claim, str(item.url))
        for item in evidence.external_sources
    ] == [(0, "selected-first", "https://example.com/duplicate")]


def test_factory_does_not_deduplicate_same_url_across_tasks() -> None:
    """同じURLでもtaskが異なる場合は、重複として除外せず両方を残す。"""
    tasks = [
        collected_task(
            task_index=0,
            research_goal="goal-A",
            external_hits=[external_hit("https://example.com/shared")],
        ),
        collected_task(
            task_index=1,
            research_goal="goal-B",
            external_hits=[external_hit("https://example.com/shared")],
        ),
    ]
    evidence = AnswerEvidence.from_reviewer_response(
        preparation=EvidenceReviewPreparation.from_tasks(tasks),
        reviewer_response=_reviewer_response(
            [
                {
                    "option_index": 0,
                    "claim": "claim-for-goal-A",
                    "why_selected": "why",
                },
                {
                    "option_index": 1,
                    "claim": "claim-for-goal-B",
                    "why_selected": "why",
                },
            ]
        ),
    )

    assert {
        (item.task_index, item.claim, str(item.url))
        for item in evidence.external_sources
    } == {
        (0, "claim-for-goal-A", "https://example.com/shared"),
        (1, "claim-for-goal-B", "https://example.com/shared"),
    }


def test_factory_keeps_first_selection_for_duplicate_curation_id_within_task() -> None:
    """同じtask内で内部検索の記事が重複した場合は、Reviewerが最初に選んだ1件だけを残す。"""
    tasks = [
        collected_task(
            task_index=0,
            internal_hits=[
                internal_hit(
                    assessment_id=1001,
                    curation_id=42,
                    title="first-copy",
                    summary="s",
                ),
                internal_hit(
                    assessment_id=1002,
                    curation_id=42,
                    title="second-copy",
                    summary="s",
                ),
            ],
        )
    ]
    evidence = AnswerEvidence.from_reviewer_response(
        preparation=EvidenceReviewPreparation.from_tasks(tasks),
        reviewer_response=_reviewer_response(
            [
                {
                    "option_index": 1,
                    "claim": "selected-first",
                    "why_selected": "why",
                },
                {
                    "option_index": 0,
                    "claim": "selected-later",
                    "why_selected": "why",
                },
            ]
        ),
    )

    assert [
        (item.task_index, item.claim, item.curation_id)
        for item in evidence.internal_articles
    ] == [(0, "selected-first", 42)]


def test_factory_does_not_deduplicate_same_curation_id_across_tasks() -> None:
    """同じ内部検索の記事でもtaskが異なる場合は、重複として除外せず両方を残す。"""
    tasks = [
        collected_task(
            task_index=0,
            research_goal="goal-A",
            internal_hits=[
                internal_hit(
                    assessment_id=1001,
                    curation_id=42,
                    title="task-0 internal",
                    summary="s",
                ),
            ],
        ),
        collected_task(
            task_index=1,
            research_goal="goal-B",
            internal_hits=[
                internal_hit(
                    assessment_id=1002,
                    curation_id=42,
                    title="task-1 internal",
                    summary="s",
                ),
            ],
        ),
    ]
    evidence = AnswerEvidence.from_reviewer_response(
        preparation=EvidenceReviewPreparation.from_tasks(tasks),
        reviewer_response=_reviewer_response(
            [
                {
                    "option_index": 0,
                    "claim": "claim-for-goal-A",
                    "why_selected": "why",
                },
                {
                    "option_index": 1,
                    "claim": "claim-for-goal-B",
                    "why_selected": "why",
                },
            ]
        ),
    )

    assert {
        (item.task_index, item.claim, item.curation_id)
        for item in evidence.internal_articles
    } == {
        (0, "claim-for-goal-A", 42),
        (1, "claim-for-goal-B", 42),
    }


def test_answer_evidence_factory_restores_option_origins_from_indexes() -> None:
    """選択indexから、出所のtask・出典情報を回答用Evidenceへ復元する。"""
    tasks = [
        collected_task(
            task_index=2,
            internal_hits=[
                internal_hit(
                    assessment_id=1001, curation_id=1, title="A-int-1", summary="s"
                ),
                internal_hit(
                    assessment_id=1002, curation_id=2, title="A-int-2", summary="s"
                ),
            ],
        ),
        collected_task(
            task_index=5,
            external_hits=[
                external_hit("https://example.com/b1", title="B-ext-1"),
                external_hit("https://example.com/b2", title="B-ext-2"),
            ],
        ),
    ]
    result = _reviewer_response(
        [
            {"option_index": 1, "claim": "A-int-2 claim", "why_selected": "why"},
            {"option_index": 3, "claim": "B-ext-2 claim", "why_selected": "why"},
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
        collected_task(
            task_index=1,
            internal_hits=[
                internal_hit(
                    assessment_id=1002, curation_id=2, title="B-int", summary="s"
                )
            ],
        ),
        collected_task(
            task_index=0,
            internal_hits=[
                internal_hit(
                    assessment_id=1001, curation_id=1, title="A-int", summary="s"
                )
            ],
        ),
    ]
    # task_index昇順(0, 1)で結合すればindex 0はA-int、1はB-intになるはず。
    result = _reviewer_response(
        [{"option_index": 0, "claim": "claim", "why_selected": "why"}],
    )

    evidence = AnswerEvidence.from_reviewer_response(
        preparation=EvidenceReviewPreparation.from_tasks(tasks),
        reviewer_response=result,
    )

    assert [(item.title, item.task_index) for item in evidence.internal_articles] == [
        ("A-int", 0)
    ]


def test_answer_evidence_factory_preserves_indexes_around_an_empty_task() -> None:
    """ヒットゼロのtaskが混ざってもindexの対応がずれない。"""
    tasks = [
        collected_task(
            task_index=0,
            internal_hits=[
                internal_hit(
                    assessment_id=1001, curation_id=1, title="A-int", summary="s"
                )
            ],
        ),
        collected_task(task_index=1),
        collected_task(
            task_index=2,
            internal_hits=[
                internal_hit(
                    assessment_id=1002, curation_id=2, title="C-int", summary="s"
                )
            ],
        ),
    ]
    result = _reviewer_response(
        [{"option_index": 1, "claim": "C-int claim", "why_selected": "why"}],
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
    """内部ヒット・選択結果・task情報から、回答用内部Evidenceの各fieldを復元する。"""
    hit = internal_hit(
        assessment_id=2001,
        curation_id=42,
        title="internal title",
        summary="internal summary",
        key_points=["key point one"],
        published_at=AS_OF,
    )
    tasks = [collected_task(task_index=1, internal_hits=[hit])]
    result = _reviewer_response(
        [{"option_index": 0, "claim": "見出しの主張", "why_selected": "選定理由"}],
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
    assert item.published_at == AS_OF
    assert item.task_index == 1
    assert item.source_ref == "1-0"


def test_answer_evidence_factory_maps_inputs_to_external_evidence_fields() -> None:
    """外部ヒット・選択結果・task情報から、回答用外部Evidenceの各fieldを復元する。"""
    hit = ExternalSearchHit(
        url=SafeUrl("https://example.com/external-story"),
        title="external title",
        snippet="external snippet",
        source_name="Example News",
        published_at=AS_OF,
    )
    tasks = [collected_task(task_index=1, external_hits=[hit])]
    result = _reviewer_response(
        [{"option_index": 0, "claim": "見出しの主張", "why_selected": "選定理由"}],
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
    assert item.published_at == AS_OF
    assert item.task_index == 1
    assert item.source_ref == "1-0"


# --- Evidence Runの確定結果 ----------------------------------------------------


def test_completed_accepts_evidence_and_reviewer_missing() -> None:
    answer_evidence = AnswerEvidence(
        internal_articles=(
            _internal_article_evidence(curation_id=1, source_ref="0-0"),
        ),
        external_sources=(
            _external_evidence(url="https://example.com/evidence", source_ref="0-1"),
        ),
    )

    result = EvidenceRunCompleted(
        answer_evidence=answer_evidence,
        review_missing=("公式発表を確認できませんでした",),
    )

    assert (result.answer_evidence, result.review_missing) == (
        answer_evidence,
        ("公式発表を確認できませんでした",),
    )


def test_completed_accepts_empty_evidence_and_empty_reviewer_missing() -> None:
    result = EvidenceRunCompleted(
        answer_evidence=AnswerEvidence(),
        review_missing=(),
    )

    assert (result.answer_evidence.count, result.review_missing) == (0, ())


@pytest.mark.parametrize(
    ("model", "field", "value"),
    [
        pytest.param(
            EvidenceRunCompleted(
                answer_evidence=AnswerEvidence(),
                review_missing=(),
            ),
            "review_missing",
            ("changed",),
            id="completed",
        ),
        pytest.param(
            EvidenceRunFailed(failure_reason="reviewer_timeout"),
            "failure_reason",
            "changed",
            id="failed",
        ),
    ],
)
def test_run_result_variants_are_frozen(
    model: EvidenceRunCompleted | EvidenceRunFailed,
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValidationError):
        setattr(model, field, value)


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        pytest.param(
            EvidenceRunCompleted,
            {
                "answer_evidence": AnswerEvidence(),
                "review_missing": (),
                "unexpected": "value",
            },
            id="completed",
        ),
        pytest.param(
            EvidenceRunFailed,
            {"failure_reason": "reviewer_timeout", "unexpected": "value"},
            id="failed",
        ),
    ],
)
def test_run_result_variants_reject_unknown_fields(
    model: type[EvidenceRunCompleted] | type[EvidenceRunFailed],
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({}, id="missing"),
        pytest.param({"failure_reason": ""}, id="empty"),
    ],
)
def test_failed_requires_a_non_empty_failure_reason(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        EvidenceRunFailed.model_validate(payload)


def test_failed_rejects_answer_evidence() -> None:
    with pytest.raises(ValidationError):
        EvidenceRunFailed.model_validate(
            {
                "failure_reason": "reviewer_timeout",
                "answer_evidence": AnswerEvidence(),
            }
        )


@pytest.mark.parametrize(
    "review_missing",
    [
        pytest.param(
            tuple(
                f"missing-{index}" for index in range(EVIDENCE_REVIEW_MISSING_LIMIT + 1)
            ),
            id="too-many-items",
        ),
        pytest.param(
            ("m" * (MISSING_ITEM_MAX_CHARS + 1),),
            id="item-too-long",
        ),
    ],
)
def test_completed_rejects_review_missing_over_reviewer_contract_caps(
    review_missing: tuple[str, ...],
) -> None:
    with pytest.raises(ValidationError):
        EvidenceRunCompleted(
            answer_evidence=AnswerEvidence(),
            review_missing=review_missing,
        )
