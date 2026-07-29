"""EvidenceCollectionOutcome の DTO 不変条件テスト(D4-S2)。

task_reports が ExternalSearchOutcome から EvidenceCollectionOutcome へ
引き上げられ、collection_failures が廃止された新契約を検証する。
InternalArticleEvidence / ResearchTaskReport は production 未実装のため
getattr ガードで参照する。
"""

from __future__ import annotations

from importlib import import_module
from types import ModuleType
from typing import Any

import pytest
from pydantic import ValidationError

from app.agent.evidence_collection import EvidenceCollectionOutcome
from app.agent.evidence_collection.external_search import (
    ExternalSearchEvidence,
    ExternalSearchOutcome,
)


def _required_attribute(module: ModuleType, name: str) -> Any:
    if not hasattr(module, name):
        pytest.fail(f"D4-S2: {module.__name__} must define {name}")
    return getattr(module, name)


def _internal_article_evidence_type() -> Any:
    try:
        contracts = import_module(
            "app.agent.evidence_collection.evidence_review.contract"
        )
    except ModuleNotFoundError as exc:
        pytest.fail(f"D4-S1 evidence_review.contract module is missing ({exc.name})")
    return _required_attribute(contracts, "InternalArticleEvidence")


def _report_type() -> Any:
    import app.agent.evidence_collection as evidence_collection_package

    return _required_attribute(evidence_collection_package, "ResearchTaskReport")


def _report(**overrides: object) -> Any:
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
        "review": "succeeded",
        "review_failure_reason": None,
        "internal_evidence_count": 0,
        "external_evidence_count": 0,
        "dropped_selection_count": 0,
        "missing": [],
    }
    values.update(overrides)
    return _report_type()(**values)


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


def test_outcome_has_no_collection_failures_field() -> None:
    """保証するテスト条件 4。run単位のcollection_failuresが廃止される。"""
    outcome = EvidenceCollectionOutcome(task_reports=[_report(task_index=0)])

    assert not hasattr(outcome, "collection_failures")


def test_outcome_rejects_legacy_collection_failures_kwarg() -> None:
    with pytest.raises(ValidationError):
        EvidenceCollectionOutcome(collection_failures=["internal_search"])


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
        EvidenceCollectionOutcome(
            task_reports=[_report(task_index=index) for index in task_indexes]
        )


def test_outcome_accepts_task_reports_covering_every_index_exactly_once() -> None:
    outcome = EvidenceCollectionOutcome(
        task_reports=[_report(task_index=0), _report(task_index=1)]
    )

    assert {report.task_index for report in outcome.task_reports} == {0, 1}


def test_outcome_rejects_internal_evidence_count_sum_that_ignores_dedup() -> None:
    """保証するテスト条件 2・5。Σ report.internal_evidence_count は

    internal_evidence / 内部dedup件数の合計と一致しなければならない。
    """
    with pytest.raises(ValidationError):
        EvidenceCollectionOutcome(
            task_reports=[_report(task_index=0, internal_evidence_count=3)],
            internal_evidence=[_internal_evidence(task_index=0)],
            internal_deduplicated_count=0,
        )


def test_outcome_rejects_external_evidence_count_sum_that_ignores_dedup() -> None:
    """保証するテスト条件 2・5。Σ report.external_evidence_count は

    external.evidence / 外部dedup件数の合計と一致しなければならない。
    """
    with pytest.raises(ValidationError):
        EvidenceCollectionOutcome(
            task_reports=[_report(task_index=0, external_evidence_count=3)],
            external_search=_external_outcome(
                [_external_evidence(task_index=0, source_ref="0-0")],
                deduplicated_evidence_count=0,
            ),
        )


def test_outcome_accepts_split_evidence_sums_including_dedup() -> None:
    """保証するテスト条件 2・5。内部Σ・外部Σはそれぞれ独立にdedup件数を含む。"""
    outcome = EvidenceCollectionOutcome(
        task_reports=[
            _report(task_index=0, internal_evidence_count=2, external_evidence_count=0),
            _report(task_index=1, internal_evidence_count=0, external_evidence_count=1),
        ],
        internal_evidence=[_internal_evidence(task_index=0)],
        internal_deduplicated_count=1,
        external_search=_external_outcome(
            [_external_evidence(task_index=1, source_ref="1-0")],
            deduplicated_evidence_count=0,
        ),
    )

    assert outcome.internal_deduplicated_count == 1
    assert sum(report.internal_evidence_count for report in outcome.task_reports) == (
        len(outcome.internal_evidence) + outcome.internal_deduplicated_count
    )
    assert sum(report.external_evidence_count for report in outcome.task_reports) == (
        len(outcome.external_search.evidence)
        + outcome.external_search.deduplicated_evidence_count
    )


def test_outcome_rejects_empty_task_reports() -> None:
    """保証するテスト条件 3。task_reportsは最低1件を要求する(min_length=1)。"""
    with pytest.raises(ValidationError):
        EvidenceCollectionOutcome(task_reports=[])


def test_outcome_has_no_unconditional_internal_hits_field() -> None:
    """段3の internal_hits(無条件採用)語彙が消えている(段4以降も維持)。"""
    outcome = EvidenceCollectionOutcome(
        task_reports=[_report(task_index=0, internal_evidence_count=1)],
        internal_evidence=[_internal_evidence()],
    )

    assert not hasattr(outcome, "internal_hits")
