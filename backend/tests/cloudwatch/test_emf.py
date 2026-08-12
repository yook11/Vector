"""``emit_count`` / ``emit_gauge`` の EMF (CloudWatch Embedded Metric Format) 出力契約。

specs/observability/cloudwatch-alerting.md §2 の書式
(root の ``_aws.Timestamp`` epoch ミリ秒 + ``_aws.CloudWatchMetrics[0]`` の
Namespace/Dimensions/Metrics、root 直下の metric 値と dimension 値) を固定する。
``emit_gauge`` は共通の ``_emit`` に委譲するだけの薄い gauge 版で、
Unit と value の型 (float) だけが ``emit_count`` (Unit=Count、value=int) と異なる。
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from app.cloudwatch.emf import emit_count, emit_gauge


def _emf_lines(captured_stdout: str) -> list[dict[str, Any]]:
    """stdout から EMF 行 (``_aws`` キーを持つ JSON 行) だけを抽出する。"""
    lines: list[dict[str, Any]] = []
    for line in captured_stdout.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and "_aws" in record:
            lines.append(record)
    return lines


def test_emit_count_writes_exactly_one_emf_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """呼び出し1回につき EMF JSON 行がちょうど 1 行だけ stdout に出る。"""
    emit_count("dispatch_run", dimensions={"cadence": "high"})

    assert len(_emf_lines(capsys.readouterr().out)) == 1


def test_emit_count_structure_matches_emf_contract(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Namespace / Dimensions / Metrics / metric 値 / dimension 値の構造を固定する。"""
    emit_count("dispatch_run", dimensions={"cadence": "high"})

    record = _emf_lines(capsys.readouterr().out)[0]
    metric_def = record["_aws"]["CloudWatchMetrics"][0]

    assert metric_def["Namespace"] == "Vector/Pipeline"
    assert metric_def["Dimensions"] == [["cadence"]]
    assert metric_def["Metrics"] == [{"Name": "dispatch_run", "Unit": "Count"}]
    assert record["dispatch_run"] == 1
    assert record["cadence"] == "high"


def test_emit_count_timestamp_is_int_epoch_ms_within_call_window(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Timestamp は呼び出し前後の epoch ミリ秒範囲内の int。"""
    before_ms = int(time.time() * 1000)
    emit_count("dispatch_run", dimensions={"cadence": "high"})
    after_ms = int(time.time() * 1000)

    record = _emf_lines(capsys.readouterr().out)[0]
    timestamp = record["_aws"]["Timestamp"]

    assert isinstance(timestamp, int)
    assert before_ms <= timestamp <= after_ms


def test_emit_count_default_value_is_one(capsys: pytest.CaptureFixture[str]) -> None:
    """value 省略時は既定 1 として emit する。"""
    emit_count("dispatch_run", dimensions={"cadence": "high"})

    record = _emf_lines(capsys.readouterr().out)[0]
    assert record["dispatch_run"] == 1


def test_emit_count_explicit_value_is_used(capsys: pytest.CaptureFixture[str]) -> None:
    """value 明示時はその値を root の metric 値として emit する。"""
    emit_count("dispatch_run", dimensions={"cadence": "high"}, value=3)

    record = _emf_lines(capsys.readouterr().out)[0]
    assert record["dispatch_run"] == 3


def test_emit_count_multiple_dimensions_are_ordered_in_dimensions_list(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """複数 dimension は挿入順で Dimensions キー列 + root 値になる。"""
    emit_count(
        "oldest_outstanding_enqueue_age",
        dimensions={"stage": "acquisition", "shard": "0"},
    )

    record = _emf_lines(capsys.readouterr().out)[0]
    metric_def = record["_aws"]["CloudWatchMetrics"][0]

    assert metric_def["Dimensions"] == [["stage", "shard"]]
    assert record["stage"] == "acquisition"
    assert record["shard"] == "0"


def test_emit_count_value_stays_int_after_shared_emit_delegation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``emit_gauge`` 追加後も value は int のまま (float 化しない)。"""
    emit_count("dispatch_run", dimensions={"cadence": "high"}, value=3)

    record = _emf_lines(capsys.readouterr().out)[0]
    assert isinstance(record["dispatch_run"], int)


def test_emit_gauge_writes_exactly_one_emf_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """呼び出し1回につき EMF JSON 行がちょうど 1 行だけ stdout に出る。"""
    emit_gauge(
        "oldest_outstanding_enqueue_age",
        dimensions={"stage": "embedding"},
        value=12.5,
        unit="Seconds",
    )

    assert len(_emf_lines(capsys.readouterr().out)) == 1


@pytest.mark.parametrize("unit", ["Seconds", "Bytes"])
def test_emit_gauge_structure_matches_emf_contract(
    unit: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Namespace/Dimensions/値の構造は emit_count と同一で、Unit だけ指定値になる。"""
    emit_gauge(
        "oldest_outstanding_enqueue_age",
        dimensions={"stage": "embedding"},
        value=12.5,
        unit=unit,
    )

    record = _emf_lines(capsys.readouterr().out)[0]
    metric_def = record["_aws"]["CloudWatchMetrics"][0]

    assert metric_def["Namespace"] == "Vector/Pipeline"
    assert metric_def["Dimensions"] == [["stage"]]
    assert metric_def["Metrics"] == [
        {"Name": "oldest_outstanding_enqueue_age", "Unit": unit}
    ]
    assert record["oldest_outstanding_enqueue_age"] == 12.5
    assert record["stage"] == "embedding"


def test_emit_gauge_value_stays_float_even_when_integer_valued(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """0.0 のような整数値でも int でなく float のまま emit される (age 0.0 契約)。"""
    emit_gauge(
        "oldest_outstanding_enqueue_age",
        dimensions={"stage": "embedding"},
        value=0.0,
        unit="Seconds",
    )

    record = _emf_lines(capsys.readouterr().out)[0]
    assert isinstance(record["oldest_outstanding_enqueue_age"], float)
    assert record["oldest_outstanding_enqueue_age"] == 0.0
