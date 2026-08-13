"""ResearchTaskReport(収集系)とEvidenceReviewReport(Run単位の精査系)の永続契約(S1)。

仕様「観測と失敗分類」により、旧ResearchTaskReportが一体で持っていた
review関連field(review / review_failure_reason / internal_evidence_count /
external_evidence_count / dropped_selection_count / missing)はRun単位の
EvidenceReviewReportへ分離されている。
"""

from __future__ import annotations

from importlib import import_module

import pytest
from pydantic import ValidationError

from app.agent.evidence_collection import ResearchTaskReport
from app.agent.evidence_review import EvidenceReviewReport


def _report(**overrides: object) -> ResearchTaskReport:
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
    return ResearchTaskReport(**values)


def _time_filter_failed_report(**overrides: object) -> ResearchTaskReport:
    values: dict[str, object] = {
        "external_collection": "time_filter_failed",
        "time_filter_failure_reason": "future_calendar_month",
    }
    values.update(overrides)
    return _report(**values)


def _query_generation_failed_report(**overrides: object) -> ResearchTaskReport:
    values: dict[str, object] = {"external_collection": "query_generation_failed"}
    values.update(overrides)
    return _report(**values)


def _provider_failed_report(**overrides: object) -> ResearchTaskReport:
    values: dict[str, object] = {
        "external_collection": "provider_failed",
        "generated_queries": ["NVIDIA 決算"],
        "provider_failed_query_count": 1,
        "external_candidate_count": 0,
    }
    values.update(overrides)
    return _report(**values)


def _review_report(**overrides: object) -> EvidenceReviewReport:
    values: dict[str, object] = {
        "review": "succeeded",
        "review_failure_reason": None,
        "internal_evidence_count": 0,
        "external_evidence_count": 0,
        "dropped_selection_count": 0,
        "missing": [],
    }
    values.update(overrides)
    return EvidenceReviewReport(**values)


# --- ResearchTaskReport(収集系) ---------------------------------------------


def test_report_accepts_the_documented_collection_shape() -> None:
    report = _report(
        internal_collection="succeeded",
        external_collection="succeeded",
        internal_candidate_count=2,
        external_candidate_count=3,
    )

    assert (
        report.internal_collection,
        report.external_collection,
        report.internal_candidate_count,
        report.external_candidate_count,
    ) == ("succeeded", "succeeded", 2, 3)


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"generated_queries": ["raw query"]}, id="generated-query"),
        pytest.param({"provider_failed_query_count": 1}, id="provider-failure"),
        pytest.param({"external_candidate_count": 1}, id="external-candidate"),
        pytest.param({"time_filter_failure_reason": None}, id="missing-reason"),
    ],
)
def test_time_filter_failed_rejects_non_closed_external_diagnostics(
    changes: dict[str, object],
) -> None:
    """保証するテスト条件 1。time_filter_failed は外部系診断だけ閉じる。"""
    with pytest.raises(ValidationError):
        _time_filter_failed_report(**changes)


def test_time_filter_failed_allows_internal_diagnostics_to_vary() -> None:
    """保証するテスト条件 1・6。time_filter_failed でも内部系は自由。"""
    report = _time_filter_failed_report(
        internal_collection="succeeded",
        internal_candidate_count=2,
    )

    assert (report.external_collection, report.internal_candidate_count) == (
        "time_filter_failed",
        2,
    )


@pytest.mark.parametrize(
    "external_collection",
    ["succeeded", "query_generation_failed", "provider_failed"],
)
def test_non_time_filter_report_rejects_time_filter_failure_reason(
    external_collection: str,
) -> None:
    with pytest.raises(ValidationError):
        _report(
            external_collection=external_collection,
            time_filter_failure_reason="future_date_range",
        )


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"generated_queries": ["raw query"]}, id="generated-query"),
        pytest.param({"provider_failed_query_count": 1}, id="provider-failure"),
        pytest.param({"external_candidate_count": 1}, id="external-candidate"),
    ],
)
def test_query_generation_failed_rejects_non_closed_external_diagnostics(
    changes: dict[str, object],
) -> None:
    """保証するテスト条件 2。query_generation_failedは外部系診断を閉じる新分類。"""
    with pytest.raises(ValidationError):
        _query_generation_failed_report(**changes)


def test_query_generation_failed_accepts_the_closed_shape() -> None:
    report = _query_generation_failed_report()

    assert report.external_collection == "query_generation_failed"


def test_provider_failed_accepts_every_generated_query_failed() -> None:
    """保証するテスト条件 2。provider_failedは生成した全queryの失敗を表す。"""
    report = _provider_failed_report()

    assert (report.external_collection, report.provider_failed_query_count) == (
        "provider_failed",
        1,
    )


def test_provider_failed_rejects_empty_generated_queries() -> None:
    """保証するテスト条件 2。provider_failedはgenerated_queriesが空を許さない

    (queryを1件も生成できていなければquery_generation_failedのはず)。
    """
    with pytest.raises(ValidationError):
        _provider_failed_report(generated_queries=[], provider_failed_query_count=0)


def test_provider_failed_rejects_partial_failure_count() -> None:
    """保証するテスト条件 2。provider_failed_query_countは生成した

    全queryの失敗数と一致しなければならない(部分失敗はprovider_failedにならない)。
    """
    with pytest.raises(ValidationError):
        _provider_failed_report(
            generated_queries=["NVIDIA 決算", "NVIDIA 供給"],
            provider_failed_query_count=1,
        )


def test_provider_failed_rejects_nonzero_external_candidate_count() -> None:
    with pytest.raises(ValidationError):
        _provider_failed_report(external_candidate_count=1)


def test_report_rejects_more_than_three_generated_queries() -> None:
    with pytest.raises(ValidationError):
        _report(generated_queries=["a", "b", "c", "d"])


def test_report_rejects_legacy_extra_field() -> None:
    with pytest.raises(ValidationError):
        ResearchTaskReport.model_validate(
            {
                "task_index": 0,
                "research_goal": "NVIDIA の根拠を確認する",
                "internal_collection": "succeeded",
                "external_collection": "succeeded",
                "collection_goal": "legacy field must not be accepted",
            }
        )


def test_report_has_no_review_related_or_legacy_fields() -> None:
    """S1(観測と失敗分類)。review関連fieldはEvidenceReviewReportへ移動した。"""
    report = _report()

    for legacy_field in (
        "review",
        "review_failure_reason",
        "internal_evidence_count",
        "external_evidence_count",
        "dropped_selection_count",
        "missing",
        "status",
        "selector_failure_reason",
        "candidate_count",
    ):
        assert not hasattr(report, legacy_field)
    for package_name in (
        "app.agent.evidence_collection",
        "app.agent.evidence_collection.external_search",
        "app.agent.evidence_collection.external_search.contract",
    ):
        package = import_module(package_name)
        assert not hasattr(package, "ResearchTaskStatus")


# --- EvidenceReviewReport(Run単位の精査系) -----------------------------------


def test_review_report_accepts_the_documented_succeeded_shape() -> None:
    report = _review_report(
        review="succeeded",
        internal_evidence_count=2,
        external_evidence_count=2,
        dropped_selection_count=1,
        missing=["公式発表が見つからない"],
    )

    assert (
        report.review,
        report.internal_evidence_count,
        report.external_evidence_count,
        report.dropped_selection_count,
        report.missing,
    ) == ("succeeded", 2, 2, 1, ["公式発表が見つからない"])


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"internal_evidence_count": 1}, id="internal-evidence"),
        pytest.param({"external_evidence_count": 1}, id="external-evidence"),
        pytest.param({"dropped_selection_count": 1}, id="dropped-selection"),
        pytest.param({"missing": ["表示用の不足理由"]}, id="missing"),
        pytest.param({"review_failure_reason": "reviewer_timeout"}, id="reason"),
    ],
)
def test_review_skipped_empty_requires_fully_closed_diagnostics(
    changes: dict[str, object],
) -> None:
    """review=skipped_emptyは内外evidence/missing/reasonが全て閉じる

    (Run全体で両候補ゼロ、reviewer未起動を表す)。
    """
    with pytest.raises(ValidationError):
        _review_report(review="skipped_empty", **changes)


def test_review_skipped_empty_accepts_the_closed_shape() -> None:
    report = _review_report(review="skipped_empty")

    assert report.review == "skipped_empty"
    assert (
        report.internal_evidence_count,
        report.external_evidence_count,
        report.dropped_selection_count,
        report.missing,
    ) == (0, 0, 0, [])


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"internal_evidence_count": 1}, id="internal-evidence"),
        pytest.param({"external_evidence_count": 1}, id="external-evidence"),
    ],
)
def test_review_failed_requires_zero_evidence_count(changes: dict[str, object]) -> None:
    """review=failedは内外双方のevidence_count=0を強制する(Run全体が根拠ゼロ)。"""
    with pytest.raises(ValidationError):
        _review_report(
            review="failed",
            review_failure_reason="reviewer_timeout",
            **changes,
        )


def test_review_failed_requires_a_failure_reason() -> None:
    with pytest.raises(ValidationError):
        _review_report(review="failed", review_failure_reason=None)


@pytest.mark.parametrize("review", ["succeeded", "skipped_empty"])
def test_non_failed_review_rejects_a_failure_reason(review: str) -> None:
    """review_failure_reason は review=failed のときだけ許される。"""
    with pytest.raises(ValidationError):
        _review_report(review=review, review_failure_reason="reviewer_timeout")


def test_review_report_rejects_evidence_count_above_the_adoption_cap() -> None:
    """内外合算がRun単位の採用上限(S2で15)を超えると拒否する。"""
    with pytest.raises(ValidationError):
        _review_report(
            review="succeeded",
            internal_evidence_count=8,
            external_evidence_count=8,
        )


def test_review_report_accepts_evidence_count_sum_at_the_adoption_cap() -> None:
    """内外合算がちょうど上限のときは受理する境界値。"""
    report = _review_report(
        review="succeeded",
        internal_evidence_count=7,
        external_evidence_count=8,
    )

    assert report.internal_evidence_count + report.external_evidence_count == 15
