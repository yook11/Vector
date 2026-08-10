"""EvidenceCollectionOutcome の DTO 不変条件テスト(S1)。

仕様「観測と失敗分類」により、Σ整合はtask_reports(収集系)の総和ではなく
review report(Run単位の精査系)と突き合わせる。task_reports自体は
0..n-1に完全対応する既存不変条件を維持する。ResearchTaskReport /
EvidenceReviewReport / InternalArticleEvidence は production 未実装のため
getattr ガードで参照する。
"""

from __future__ import annotations

from importlib import import_module
from types import ModuleType
from typing import Any

import pytest
from pydantic import ValidationError

from app.agent.evidence_collection.external_search import (
    ExternalSearchEvidence,
    ExternalSearchOutcome,
)
from app.agent.evidence_review import EvidenceCollectionOutcome


def _required_attribute(module: ModuleType, name: str) -> Any:
    if not hasattr(module, name):
        pytest.fail(f"S1: {module.__name__} must define {name}")
    return getattr(module, name)


def _internal_article_evidence_type() -> Any:
    try:
        contracts = import_module("app.agent.evidence_review.contract")
    except ModuleNotFoundError as exc:
        pytest.fail(f"evidence_review.contract module is missing ({exc.name})")
    return _required_attribute(contracts, "InternalArticleEvidence")


def _report_type() -> Any:
    import app.agent.evidence_collection as evidence_collection_package

    return _required_attribute(evidence_collection_package, "ResearchTaskReport")


def _review_report_type() -> Any:
    import app.agent.evidence_review as evidence_review_package

    return _required_attribute(evidence_review_package, "EvidenceReviewReport")


def _report(**overrides: object) -> Any:
    """S1: ResearchTaskReportは収集系だけを持つ(review関連はEvidenceReviewReportへ)。"""
    values: dict[str, object] = {
        "task_index": 0,
        "research_goal": "NVIDIA の供給を確認する",
        "internal_collection": "succeeded",
        "external_collection": "succeeded",
        "time_filter_failure_reason": None,
        "generated_queries": [],
        "provider_failed_query_count": 0,
        "internal_candidate_count": 0,
        "external_candidate_count": 0,
    }
    values.update(overrides)
    return _report_type()(**values)


def _review_report(**overrides: object) -> Any:
    values: dict[str, object] = {
        "review": "succeeded",
        "review_failure_reason": None,
        "internal_evidence_count": 0,
        "external_evidence_count": 0,
        "dropped_selection_count": 0,
        "missing": [],
    }
    values.update(overrides)
    return _review_report_type()(**values)


def _external_outcome(
    evidence: list[ExternalSearchEvidence] | None = None,
    *,
    deduplicated_evidence_count: int = 0,
) -> ExternalSearchOutcome:
    return ExternalSearchOutcome(
        evidence=evidence or [],
        deduplicated_evidence_count=deduplicated_evidence_count,
        effective_agent_count=0,
    )


def _internal_evidence(*, curation_id: int = 1, task_index: int = 0) -> Any:
    evidence_type = _internal_article_evidence_type()
    return evidence_type(
        source_ref=f"{task_index}-0",
        task_index=task_index,
        claim="claim",
        why_selected="why",
        assessment_id=1001,
        curation_id=curation_id,
        title="NVIDIA",
        summary="NVIDIA summary",
        key_points=[],
        published_at=None,
    )


def _external_evidence(*, task_index: int, source_ref: str) -> ExternalSearchEvidence:
    return ExternalSearchEvidence(
        source_ref=source_ref,
        task_index=task_index,
        claim="claim",
        why_selected="why",
        url="https://example.com/evidence",
        title="evidence",
    )


def _outcome(
    *, task_reports: list[Any], review: Any | None = None, **kwargs: object
) -> Any:
    return EvidenceCollectionOutcome(
        task_reports=task_reports,
        review=review if review is not None else _review_report(),
        **kwargs,
    )


def test_outcome_has_no_collection_failures_field() -> None:
    """保証するテスト条件 4。run単位のcollection_failuresが廃止される。"""
    outcome = _outcome(task_reports=[_report(task_index=0)])

    assert not hasattr(outcome, "collection_failures")


def test_outcome_rejects_legacy_collection_failures_kwarg() -> None:
    with pytest.raises(ValidationError):
        EvidenceCollectionOutcome(
            task_reports=[_report(task_index=0)],
            review=_review_report(),
            collection_failures=["internal_search"],
        )


@pytest.mark.parametrize(
    "task_indexes",
    [
        # [0]([0]のみのn=1構成)はreport_indexes=={0}==set(range(1))を満たし
        # 構造的に常にvalidなためケースから除く(このvalidatorは自己完結で、
        # 外部のplan task数と突き合わせる手段を持たない)。
        pytest.param([0, 0], id="duplicate"),
        pytest.param([0, 2], id="missing"),
    ],
)
def test_outcome_rejects_task_reports_that_do_not_cover_0_to_n_1(
    task_indexes: list[int],
) -> None:
    """保証するテスト条件 1。task_reports は 0..n-1 に完全対応する。"""
    with pytest.raises(ValidationError):
        _outcome(task_reports=[_report(task_index=index) for index in task_indexes])


def test_outcome_accepts_task_reports_covering_every_index_exactly_once() -> None:
    outcome = _outcome(task_reports=[_report(task_index=0), _report(task_index=1)])

    assert {report.task_index for report in outcome.task_reports} == {0, 1}


def test_outcome_rejects_internal_evidence_count_sum_that_ignores_dedup() -> None:
    """S1(観測と失敗分類)。Σはreview.internal_evidence_countと

    internal_evidence / 内部dedup件数の合計の突き合わせに変わる
    (task_reportsの総和ではない)。
    """
    with pytest.raises(ValidationError):
        _outcome(
            task_reports=[_report(task_index=0)],
            review=_review_report(internal_evidence_count=3),
            internal_evidence=[_internal_evidence(task_index=0)],
            internal_deduplicated_count=0,
        )


def test_outcome_rejects_external_evidence_count_sum_that_ignores_dedup() -> None:
    """S1(観測と失敗分類)。Σはreview.external_evidence_countと

    external.evidence / 外部dedup件数の合計の突き合わせに変わる。
    """
    with pytest.raises(ValidationError):
        _outcome(
            task_reports=[_report(task_index=0)],
            review=_review_report(external_evidence_count=3),
            external_search=_external_outcome(
                [_external_evidence(task_index=0, source_ref="0-0")],
                deduplicated_evidence_count=0,
            ),
        )


def test_outcome_accepts_split_evidence_sums_including_dedup() -> None:
    """review.internal_evidence_count・review.external_evidence_countは

    それぞれ独立にdedup件数を含む(Run単位、task_reportsを合算しない)。
    """
    outcome = _outcome(
        task_reports=[_report(task_index=0), _report(task_index=1)],
        review=_review_report(internal_evidence_count=2, external_evidence_count=1),
        internal_evidence=[_internal_evidence(task_index=0)],
        internal_deduplicated_count=1,
        external_search=_external_outcome(
            [_external_evidence(task_index=1, source_ref="1-0")],
            deduplicated_evidence_count=0,
        ),
    )

    assert outcome.internal_deduplicated_count == 1
    assert outcome.review.internal_evidence_count == (
        len(outcome.internal_evidence) + outcome.internal_deduplicated_count
    )
    assert outcome.review.external_evidence_count == (
        len(outcome.external_search.evidence)
        + outcome.external_search.deduplicated_evidence_count
    )


def test_outcome_rejects_empty_task_reports() -> None:
    """保証するテスト条件 3。task_reportsは最低1件を要求する(min_length=1)。"""
    with pytest.raises(ValidationError):
        _outcome(task_reports=[])


def test_outcome_has_no_unconditional_internal_hits_field() -> None:
    """段3の internal_hits(無条件採用)語彙が消えている(段4以降も維持)。"""
    outcome = _outcome(
        task_reports=[_report(task_index=0)],
        review=_review_report(internal_evidence_count=1),
        internal_evidence=[_internal_evidence()],
    )

    assert not hasattr(outcome, "internal_hits")
