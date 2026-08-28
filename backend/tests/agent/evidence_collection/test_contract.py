"""ResearchTaskReportが収集工程の情報だけを持つ契約テスト。"""

from __future__ import annotations

from importlib import import_module

import pytest
from pydantic import ValidationError

from app.agent.evidence_collection import (
    CollectedNews,
    CollectedTask,
    ResearchTaskReport,
)


def _report(**overrides: object) -> ResearchTaskReport:
    """ResearchTaskReportは収集工程だけを表す。"""
    values: dict[str, object] = {
        "task_index": 0,
        "research_goal": "NVIDIA の供給を確認する",
        "internal_collection": "succeeded",
        "external_collection": "succeeded",
        "generated_queries": [],
        "provider_failed_query_count": 0,
        "internal_hit_count": 0,
        "external_hit_count": 0,
    }
    values.update(overrides)
    return ResearchTaskReport(**values)


def _query_generation_failed_report(**overrides: object) -> ResearchTaskReport:
    values: dict[str, object] = {"external_collection": "query_generation_failed"}
    values.update(overrides)
    return _report(**values)


def _provider_failed_report(**overrides: object) -> ResearchTaskReport:
    values: dict[str, object] = {
        "external_collection": "provider_failed",
        "generated_queries": ["NVIDIA 決算"],
        "provider_failed_query_count": 1,
        "external_hit_count": 0,
    }
    values.update(overrides)
    return _report(**values)


def _collected_task(
    *,
    task_index: int,
    report_task_index: int | None = None,
) -> CollectedTask:
    return CollectedTask(
        task_index=task_index,
        research_goal=f"goal-{task_index}",
        internal_hits=[],
        external_hits=[],
        executed_queries=(),
        report=_report(
            task_index=task_index if report_task_index is None else report_task_index,
            research_goal=f"goal-{task_index}",
        ),
    )


# --- ResearchTaskReport(収集系) ---------------------------------------------


def test_report_accepts_the_documented_collection_shape() -> None:
    report = _report(
        internal_collection="succeeded",
        external_collection="succeeded",
        internal_hit_count=2,
        external_hit_count=3,
    )

    assert (
        report.internal_collection,
        report.external_collection,
        report.internal_hit_count,
        report.external_hit_count,
    ) == ("succeeded", "succeeded", 2, 3)


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"generated_queries": ["raw query"]}, id="generated-query"),
        pytest.param({"provider_failed_query_count": 1}, id="provider-failure"),
        pytest.param({"external_hit_count": 1}, id="external-hit"),
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


def test_provider_failed_rejects_nonzero_external_hit_count() -> None:
    with pytest.raises(ValidationError):
        _provider_failed_report(external_hit_count=1)


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
    """収集Reportへ精査結果・失敗理由を混ぜない。"""
    report = _report()

    for legacy_field in (
        "review",
        "review_failure_code",
        "internal_evidence_count",
        "external_evidence_count",
        "dropped_selection_count",
        "missing",
        "status",
        "selector_failure_reason",
        "hit_count",
        "time_filter_failure_reason",
    ):
        assert not hasattr(report, legacy_field)
    for package_name in (
        "app.agent.evidence_collection",
        "app.agent.evidence_collection.external_search",
        "app.agent.evidence_collection.external_search.contract",
    ):
        package = import_module(package_name)
        assert not hasattr(package, "ResearchTaskStatus")
        assert not hasattr(package, "EXTERNAL_SEARCH_AGENT_HARD_LIMIT")
        assert not hasattr(package, "resolve_external_search_agent_count")


# --- CollectedNews (収集Run) -------------------------------------------------


def test_collected_news_accepts_contiguous_matching_task_indexes() -> None:
    collected_news = CollectedNews(
        tasks=[_collected_task(task_index=0), _collected_task(task_index=1)],
    )

    assert [task.task_index for task in collected_news.tasks] == [0, 1]
    assert not hasattr(collected_news, "requested_agent_count")
    assert not hasattr(collected_news, "effective_agent_count")


@pytest.mark.parametrize(
    "task_indexes",
    [
        pytest.param([0, 0], id="duplicate"),
        pytest.param([0, 2], id="missing"),
        pytest.param([1], id="does-not-start-at-zero"),
    ],
)
def test_collected_news_rejects_non_contiguous_or_duplicate_task_indexes(
    task_indexes: list[int],
) -> None:
    with pytest.raises(ValueError):
        CollectedNews(
            tasks=[_collected_task(task_index=index) for index in task_indexes],
        )


def test_collected_news_rejects_task_and_report_index_mismatch() -> None:
    with pytest.raises(ValueError):
        CollectedNews(
            tasks=[_collected_task(task_index=0, report_task_index=1)],
        )
