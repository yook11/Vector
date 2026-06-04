"""capfire から ``article_stage`` span を取り出す共有 helper。

各テストは期待値を仕様から直書きする。本 helper は span の抽出だけを担い、
属性の期待値は持たない (再実装による tautology を避ける)。
"""

from __future__ import annotations

from typing import Any

from logfire.testing import CaptureLogfire

_SPAN_NAME = "article_stage"

# logfire が全 span に自動付与する framework attribute の prefix。
# - ``logfire.`` … msg / span_type / json_schema など。
# - ``code.`` … span を開いた call site の filepath / function / lineno (helper
#   自身のソース位置で固定。記事データは含まない)。
# ドメイン attribute との切り分けに使う。
_FRAMEWORK_ATTR_PREFIXES = ("logfire.", "code.")


def article_stage_spans(capfire: CaptureLogfire) -> list[dict[str, Any]]:
    """exporter に出た ``article_stage`` span を出現順に返す。"""
    return [
        s for s in capfire.exporter.exported_spans_as_dict() if s["name"] == _SPAN_NAME
    ]


def one_article_stage_span(capfire: CaptureLogfire) -> dict[str, Any]:
    """``article_stage`` span がちょうど 1 件あることを確認して返す。"""
    spans = article_stage_spans(capfire)
    assert len(spans) == 1, f"expected exactly 1 article_stage span, got {len(spans)}"
    return spans[0]


def stage_attrs(capfire: CaptureLogfire) -> dict[str, Any]:
    """ちょうど 1 件の ``article_stage`` span の attributes を返す。"""
    return one_article_stage_span(capfire)["attributes"]


def domain_attr_keys(attrs: dict[str, Any]) -> set[str]:
    """framework attribute を除いたドメイン attribute のキー集合を返す。"""
    return {k for k in attrs if not k.startswith(_FRAMEWORK_ATTR_PREFIXES)}
