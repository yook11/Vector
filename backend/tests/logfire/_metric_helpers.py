"""capfire が収集した counter metric を取り出す共有 helper。

各テストは期待値を仕様から直書きする。``collected_metrics`` /
``counter_attribute_key_sets`` / ``sum_counter_for_result`` は metric の抽出だけを
担い、期待値は持たない (再実装による tautology を避ける)。``get_collected_metrics``
は 0 件時に内部 AttributeError を投げるため [] に畳む。``assert_attribute_contract``
は例外的に assert 自体を担う helper で、attribute の許可値域 (ホワイトリスト) を
呼び出し側の仕様定数から受け取って検証する。
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
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


def attributes_of(metrics: list[dict[str, Any]], name: str) -> dict[str, Any]:
    """``name`` metric の唯一の data point が持つ attribute dict を返す。

    呼び出し側が ``== {...}`` で突き合わせると、attribute key 集合の正確さと、
    recorder へ渡した値が載ったこと (伝播) を 1 つの assert で固定できる。emit を 1 回
    だけ起こすテスト用で、data point が 0 件 / 複数件なら期待が崩れるため fail する。
    """
    metric = _find(metrics, name)
    assert metric is not None, f"{name} が exporter に届かない"
    data_points = metric["data"]["data_points"]
    assert len(data_points) == 1, f"{name} の data point が {len(data_points)} 件"
    return dict(data_points[0].get("attributes", {}))


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


def assert_attribute_contract(
    metrics: list[dict[str, Any]],
    name: str,
    *,
    allowed: Mapping[str, Collection[str]],
) -> None:
    """``name`` counter の全 data point が attribute 契約を満たすことを assert する。

    契約は「attribute key 集合が ``allowed`` の key 集合と一致し、各値が対応する
    許可値域 (Collection) に含まれる」こと。未知の値は網羅不能な needle blocklist
    ではなく許可値域 (whitelist) で落とすため、``allowed`` は Literal/StrEnum 等の
    仕様 SSoT から呼び出し側が導出する。metric 未収集や data point 0 件は
    非空虚性を壊すため fail 扱いにする。
    """
    metric = _find(metrics, name)
    assert metric is not None, f"{name} が exporter に届かない"
    data_points = metric["data"]["data_points"]
    assert data_points, f"{name} に data point が無い"
    expected_keys = set(allowed.keys())
    for dp in data_points:
        attrs = dp.get("attributes", {})
        assert set(attrs.keys()) == expected_keys, (
            f"{name} unexpected attribute keys: {set(attrs.keys())}"
        )
        for key, allowed_values in allowed.items():
            assert attrs[key] in allowed_values, (
                f"{name} attribute {key!r} value {attrs[key]!r} not in "
                f"{allowed_values!r}"
            )
