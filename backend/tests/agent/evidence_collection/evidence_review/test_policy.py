"""Evidence Reviewer のドメイン純関数契約(D4-S1)。

統合candidate projectionの構築と、index→出典の再構築(drop/dedup/cap)を
検証する。production 未実装のため getattr ガードで参照する。
"""

from __future__ import annotations

from datetime import UTC, datetime
from importlib import import_module
from types import ModuleType
from typing import Any

import pytest

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
        return import_module("app.agent.evidence_collection.evidence_review.policy")
    except ModuleNotFoundError as exc:
        pytest.fail(
            "D4-S1 evidence_review のドメイン純関数は policy module に置く必要が"
            f"あります ({exc.name})",
            pytrace=False,
        )


def _function(name: str) -> Any:
    value = getattr(_policy(), name, None)
    if value is None:
        pytest.fail(f"evidence_review policy must export {name}", pytrace=False)
    return value


def _contracts() -> ModuleType:
    return import_module("app.agent.evidence_collection.evidence_review.contract")


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


def test_resolve_reviewer_failure_reason_prefers_reason_then_code_then_fallback() -> (
    None
):
    resolve_failure_reason = _function("resolve_reviewer_failure_reason")

    assert (
        resolve_failure_reason(reason="timeout", code="ai_error_network"),
        resolve_failure_reason(reason=None, code="ai_error_network"),
        resolve_failure_reason(reason=None, code=None),
    ) == ("timeout", "ai_error_network", "reviewer_error")


def test_projection_places_internal_candidates_before_external_in_one_index_space() -> (
    None
):
    """保証するテスト条件 1。内部先・0始まりの単一index空間。"""
    build_projection = _function("build_review_candidate_projection")
    internal_hits = [
        _internal_hit(
            assessment_id=1001, curation_id=1, title="internal-a", summary="summary-a"
        ),
        _internal_hit(
            assessment_id=1002, curation_id=2, title="internal-b", summary="summary-b"
        ),
    ]
    external_candidates = [
        _external_candidate("https://example.com/x", title="external-x"),
        _external_candidate("https://example.com/y", title="external-y"),
    ]

    projection = build_projection(
        internal_hits=internal_hits,
        external_candidates=external_candidates,
    )

    assert [(candidate.index, candidate.title) for candidate in projection] == [
        (0, "internal-a"),
        (1, "internal-b"),
        (2, "external-x"),
        (3, "external-y"),
    ]


def test_projection_maps_internal_source_name_to_none_and_truncates_snippet() -> None:
    """内部候補: source_name=None、snippetはsummary+key_points連結をcapで truncate。"""
    build_projection = _function("build_review_candidate_projection")
    overlong_summary = "s" * (CANDIDATE_SNIPPET_MAX_CHARS + 50)
    hit = _internal_hit(
        assessment_id=1001,
        curation_id=1,
        title="internal",
        summary=overlong_summary,
        key_points=["point-a"],
        published_at=_AS_OF,
    )

    projection = build_projection(internal_hits=[hit], external_candidates=[])

    candidate = projection[0]
    assert candidate.source_name is None
    assert candidate.published_at == _AS_OF
    assert len(candidate.snippet) == CANDIDATE_SNIPPET_MAX_CHARS
    assert candidate.snippet == overlong_summary[:CANDIDATE_SNIPPET_MAX_CHARS]


def test_projection_is_empty_when_both_origins_are_empty() -> None:
    build_projection = _function("build_review_candidate_projection")

    assert build_projection(internal_hits=[], external_candidates=[]) == ()


def test_build_evidence_drops_out_of_range_index_across_both_origins() -> None:
    """保証するテスト条件 4。統合index空間の範囲外(内部件数+外部件数以上)をdrop。"""
    build_evidence = _function("build_review_evidence")
    internal_hits = [
        _internal_hit(assessment_id=1001, curation_id=1, title="internal", summary="s")
    ]
    external_candidates = [_external_candidate("https://example.com/only")]
    result = _review_result(
        _policy(),
        [
            {"candidate_index": 0, "claim": "internal claim", "why_selected": "why"},
            {"candidate_index": 1, "claim": "external claim", "why_selected": "why"},
            {"candidate_index": 2, "claim": "out of range", "why_selected": "why"},
        ],
    )

    internal_evidence, external_evidence, dropped = build_evidence(
        task_index=0,
        internal_hits=internal_hits,
        external_candidates=external_candidates,
        selection_result=result,
    )

    assert (
        [item.claim for item in internal_evidence],
        [item.claim for item in external_evidence],
        dropped,
    ) == (["internal claim"], ["external claim"], 1)


def test_build_evidence_drops_duplicate_index_and_caps_at_task_limit() -> None:
    """保証するテスト条件 4。重複indexと採用上限(5件)超過をdrop。"""
    build_evidence = _function("build_review_evidence")
    internal_hits = [
        _internal_hit(
            assessment_id=1000 + index,
            curation_id=index + 1,
            title=f"internal-{index}",
            summary="s",
        )
        for index in range(3)
    ]
    external_candidates = [
        _external_candidate(f"https://example.com/{index}") for index in range(4)
    ]
    # 統合index空間: 0-2が内部、3-6が外部の合計7候補。
    selections = [
        {"candidate_index": index, "claim": f"claim-{index}", "why_selected": "why"}
        for index in [0, 0, 1, 2, 3, 4, 5, 6]
    ]
    result = _review_result(_policy(), selections)

    internal_evidence, external_evidence, dropped = build_evidence(
        task_index=0,
        internal_hits=internal_hits,
        external_candidates=external_candidates,
        selection_result=result,
    )

    assert (
        len(internal_evidence) + len(external_evidence),
        dropped,
    ) == (5, 3)


def test_build_evidence_assigns_task_scoped_source_ref_without_origin_prefix() -> None:
    """統合index空間のsource_refは f"{task_index}-{candidate_index}"。"""
    build_evidence = _function("build_review_evidence")
    internal_hits = [
        _internal_hit(
            assessment_id=1001,
            curation_id=7,
            title="internal",
            summary="internal summary",
            key_points=["point"],
        )
    ]
    external_candidates = [_external_candidate("https://example.com/ext")]
    result = _review_result(
        _policy(),
        [
            {"candidate_index": 0, "claim": "internal claim", "why_selected": "why-a"},
            {"candidate_index": 1, "claim": "external claim", "why_selected": "why-b"},
        ],
    )

    internal_evidence, external_evidence, _dropped = build_evidence(
        task_index=4,
        internal_hits=internal_hits,
        external_candidates=external_candidates,
        selection_result=result,
    )

    assert internal_evidence[0].source_ref == "4-0"
    assert external_evidence[0].source_ref == "4-1"
    assert not internal_evidence[0].source_ref.startswith("external-")
    assert not internal_evidence[0].source_ref.startswith("internal-")


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
    result = _review_result(
        _policy(),
        [{"candidate_index": 0, "claim": "見出しの主張", "why_selected": "選定理由"}],
    )

    internal_evidence, external_evidence, dropped = build_evidence(
        task_index=1,
        internal_hits=[hit],
        external_candidates=[],
        selection_result=result,
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
