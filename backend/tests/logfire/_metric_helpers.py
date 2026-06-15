"""capfire が収集した counter metric を取り出す共有 helper。

各テストは期待値を仕様から直書きする。本 helper は metric の抽出だけを担い、
期待値は持たない (再実装による tautology を避ける)。``get_collected_metrics`` は
0 件時に内部 AttributeError を投げるため [] に畳む。
"""

from __future__ import annotations

from typing import Any

from logfire.testing import CaptureLogfire


def collected_metrics(capfire: CaptureLogfire) -> list[dict[str, Any]]:
    """収集済み metric を返す。0 件時の内部 AttributeError は [] に畳む。"""
    try:
        return capfire.get_collected_metrics()
    except AttributeError:
        return []


def _find(metrics: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next((m for m in metrics if m["name"] == name), None)


def sum_counter_for_result(
    metrics: list[dict[str, Any]], name: str, result: str
) -> int:
    """``name`` counter の ``result`` 別 data point 合計値。未収集なら 0。"""
    metric = _find(metrics, name)
    if metric is None:
        return 0
    return sum(
        int(dp["value"])
        for dp in metric["data"]["data_points"]
        if dp.get("attributes", {}).get("result") == result
    )


def counter_attribute_key_sets(
    metrics: list[dict[str, Any]], name: str
) -> list[set[str]]:
    """``name`` counter の各 data point の attribute key 集合。未収集なら []。"""
    metric = _find(metrics, name)
    if metric is None:
        return []
    return [
        set(dp.get("attributes", {}).keys()) for dp in metric["data"]["data_points"]
    ]
