"""``emit_metric`` の EMF (CloudWatch Embedded Metric Format) 出力契約。

specs/observability/cloudwatch-alerting.md §2 の書式
(root の ``_aws.Timestamp`` epoch ミリ秒 + ``_aws.CloudWatchMetrics[0]`` の
Namespace/Dimensions/Metrics、root 直下の metric 値と dimension 値) を固定する。
count / gauge の区別は EMF に存在しないため API は 1 本で、Unit は常に呼び出し側が
指定し、value の型 (int / float) はそのまま JSON に載る。
"""

from __future__ import annotations

import time

import pytest

from app.cloudwatch.emf import emit_metric
from tests.cloudwatch.records import emf_records


def test_emit_metric_writes_exactly_one_emf_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """呼び出し1回につき EMF JSON 行がちょうど 1 行だけ stdout に出る。"""
    emit_metric("dispatch_run", dimensions={"cadence": "high"}, value=1, unit="Count")

    assert len(emf_records(capsys.readouterr().out)) == 1


def test_emit_metric_structure_matches_emf_contract(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Namespace / Dimensions / Metrics / metric 値 / dimension 値の構造を固定する。"""
    emit_metric("dispatch_run", dimensions={"cadence": "high"}, value=1, unit="Count")

    record = emf_records(capsys.readouterr().out)[0]
    metric_def = record["_aws"]["CloudWatchMetrics"][0]

    assert metric_def["Namespace"] == "Vector/Pipeline"
    assert metric_def["Dimensions"] == [["cadence"]]
    assert metric_def["Metrics"] == [{"Name": "dispatch_run", "Unit": "Count"}]
    assert record["dispatch_run"] == 1
    assert record["cadence"] == "high"


def test_emit_metric_timestamp_is_int_epoch_ms_within_call_window(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Timestamp は呼び出し前後の epoch ミリ秒範囲内の int。"""
    before_ms = int(time.time() * 1000)
    emit_metric("dispatch_run", dimensions={"cadence": "high"}, value=1, unit="Count")
    after_ms = int(time.time() * 1000)

    record = emf_records(capsys.readouterr().out)[0]
    timestamp = record["_aws"]["Timestamp"]

    assert isinstance(timestamp, int)
    assert before_ms <= timestamp <= after_ms


@pytest.mark.parametrize("unit", ["Seconds", "Bytes"])
def test_emit_metric_unit_is_caller_specified(
    unit: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Unit は既定値を持たず、常に呼び出し側の指定値がそのまま載る。"""
    emit_metric(
        "oldest_outstanding_enqueue_age",
        dimensions={"stage": "embedding"},
        value=12.5,
        unit=unit,
    )

    record = emf_records(capsys.readouterr().out)[0]
    metric_def = record["_aws"]["CloudWatchMetrics"][0]

    assert metric_def["Metrics"] == [
        {"Name": "oldest_outstanding_enqueue_age", "Unit": unit}
    ]
    assert record["oldest_outstanding_enqueue_age"] == 12.5


def test_emit_metric_explicit_value_is_used(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """渡した value がそのまま root の metric 値として emit される。"""
    emit_metric("dispatch_run", dimensions={"cadence": "high"}, value=3, unit="Count")

    record = emf_records(capsys.readouterr().out)[0]
    assert record["dispatch_run"] == 3


def test_emit_metric_int_value_stays_int(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """int を渡したときは float 化されず int のまま emit される。"""
    emit_metric("dispatch_run", dimensions={"cadence": "high"}, value=3, unit="Count")

    record = emf_records(capsys.readouterr().out)[0]
    assert isinstance(record["dispatch_run"], int)


def test_emit_metric_float_value_stays_float_even_when_integer_valued(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """0.0 のような整数値でも int でなく float のまま emit される (age 0.0 契約)。"""
    emit_metric(
        "oldest_outstanding_enqueue_age",
        dimensions={"stage": "embedding"},
        value=0.0,
        unit="Seconds",
    )

    record = emf_records(capsys.readouterr().out)[0]
    assert isinstance(record["oldest_outstanding_enqueue_age"], float)
    assert record["oldest_outstanding_enqueue_age"] == 0.0


def test_emit_metric_multiple_dimensions_are_ordered_in_dimensions_list(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """複数 dimension は挿入順で Dimensions キー列 + root 値になる。"""
    emit_metric(
        "oldest_outstanding_enqueue_age",
        dimensions={"stage": "acquisition", "shard": "0"},
        value=1.0,
        unit="Seconds",
    )

    record = emf_records(capsys.readouterr().out)[0]
    metric_def = record["_aws"]["CloudWatchMetrics"][0]

    assert metric_def["Dimensions"] == [["stage", "shard"]]
    assert record["stage"] == "acquisition"
    assert record["shard"] == "0"
