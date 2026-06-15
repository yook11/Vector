"""``vector.curation.outcome`` counter の不変条件 (正本)。

curation の span result から派生する運用可視化 metric。span は5値 (signal/noise/
rate_limited/skipped/failed) を保つが、counter は dashboard 対象の3値 (signal/noise/
failed) のみを emit する。本ファイルは counter の emit 契約を固定する (span 属性側の
語彙反映は ``test_article_stage.py`` が正本)。

emit は ``CurationStageSpan.set_result`` の no-override guard 内側の1点。capfire の
収集 metric を oracle にし、3値の emit・非対象値の非 emit・no-override 二重計上防止・
attribute=result のみ を検証する。capfire は内部で
``logfire.configure(send_to_logfire=False, ...)`` を呼ぶため setup_logfire は呼ばない。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from logfire.testing import CaptureLogfire

from app.logfire.article_stage import curation_stage_span

_METRIC = "vector.curation.outcome"


def _collected(capfire: CaptureLogfire) -> list[dict[str, Any]]:
    """収集済み metric を返す。0 件時の内部 AttributeError は [] に畳む。"""
    try:
        return capfire.get_collected_metrics()
    except AttributeError:
        return []


def _find_metric(metrics: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next((m for m in metrics if m["name"] == name), None)


def _sum_for_result(metrics: list[dict[str, Any]], result: str) -> int:
    """``vector.curation.outcome`` の result 別 data point の合計値。"""
    m = _find_metric(metrics, _METRIC)
    if m is None:
        return 0
    return sum(
        int(dp["value"])
        for dp in m["data"]["data_points"]
        if dp.get("attributes", {}).get("result") == result
    )


def _attribute_key_sets(metrics: list[dict[str, Any]]) -> list[set[str]]:
    """``vector.curation.outcome`` の各 data point の attribute key 集合。"""
    m = _find_metric(metrics, _METRIC)
    if m is None:
        return []
    return [set(dp.get("attributes", {}).keys()) for dp in m["data"]["data_points"]]


# 不変条件 1: 3値の emit (signal / noise / failed)


def test_signal_increments_signal_only(capfire: CaptureLogfire) -> None:
    """set_result(signal) で result=signal が +1、noise/failed は 0。"""
    with curation_stage_span(article_id=1) as stage:
        stage.set_result("signal")
    metrics = _collected(capfire)
    assert _sum_for_result(metrics, "signal") == 1
    assert _sum_for_result(metrics, "noise") == 0
    assert _sum_for_result(metrics, "failed") == 0


def test_noise_increments_noise_only(capfire: CaptureLogfire) -> None:
    """set_result(noise) で result=noise が +1。"""
    with curation_stage_span(article_id=1) as stage:
        stage.set_result("noise")
    metrics = _collected(capfire)
    assert _sum_for_result(metrics, "noise") == 1
    assert _sum_for_result(metrics, "signal") == 0
    assert _sum_for_result(metrics, "failed") == 0


def test_exception_backstop_increments_failed(capfire: CaptureLogfire) -> None:
    """result 未設定で例外貫通 → backstop の failed が counter にも +1。"""
    with pytest.raises(ValueError, match="boom"):
        with curation_stage_span(article_id=1):
            raise ValueError("boom")
    metrics = _collected(capfire)
    assert _sum_for_result(metrics, "failed") == 1


# 不変条件 2: dashboard 対象外 (rate_limited / skipped) は emit しない


@pytest.mark.parametrize("result", ["rate_limited", "skipped"])
def test_non_dashboard_result_not_counted(capfire: CaptureLogfire, result: str) -> None:
    """rate_limited / skipped は span には載るが counter には emit されない。

    別 span で signal を 1 件出して metric dump を非空にしつつ、対象外 result の
    data point が 0 であることを固定する (全5値を naive に emit する実装を落とす)。
    """
    with curation_stage_span(article_id=1) as stage:
        stage.set_result(result)  # type: ignore[arg-type]
    with curation_stage_span(article_id=2) as stage:
        stage.set_result("signal")
    metrics = _collected(capfire)
    assert _sum_for_result(metrics, result) == 0
    assert _sum_for_result(metrics, "signal") == 1


# 不変条件 3: no-override (1 span → 0 or 1 increment)


def test_no_override_counts_first_result_only(capfire: CaptureLogfire) -> None:
    """同一 span で複数回 set_result しても最初の result だけ counter に反映する。"""
    with curation_stage_span(article_id=1) as stage:
        stage.set_result("signal")
        stage.set_result("noise")
    metrics = _collected(capfire)
    assert _sum_for_result(metrics, "signal") == 1
    assert _sum_for_result(metrics, "noise") == 0


def test_backstop_after_result_does_not_double_count(
    capfire: CaptureLogfire,
) -> None:
    """result 確定後に例外 → backstop の failed は counter を二重計上しない。"""
    with pytest.raises(RuntimeError, match="kiq down"):
        with curation_stage_span(article_id=1) as stage:
            stage.set_result("signal")
            raise RuntimeError("kiq down")
    metrics = _collected(capfire)
    assert _sum_for_result(metrics, "signal") == 1
    assert _sum_for_result(metrics, "failed") == 0


# 不変条件 4: attribute safety (result のみ、PII 非含有)


def test_attribute_is_result_only_no_pii(capfire: CaptureLogfire) -> None:
    """counter の全 data point attribute keys が {"result"} のみで PII を載せない。"""
    with curation_stage_span(article_id=1) as stage:
        stage.set_result("signal")
    metrics = _collected(capfire)
    key_sets = _attribute_key_sets(metrics)
    assert key_sets == [{"result"}], f"unexpected attribute keys: {key_sets}"
    dumped = json.dumps(metrics, default=str, ensure_ascii=False)
    for needle in (
        "article_id",
        "source_id",
        "http://",
        "https://",
        "prompt",
        "raw_response",
    ):
        assert needle not in dumped, f"PII 様文字列 {needle!r} が metric dump に混入"
