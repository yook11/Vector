"""capfire から stage span を取り出す共有 helper。

``article_stage`` (AI 3 工程) と ``pipeline_stage`` (非 AI worker 工程) の両方を
span 名で取り分ける。各テストは期待値を仕様から直書きする。本 helper は span の
抽出だけを担い、属性の期待値は持たない (再実装による tautology を避ける)。
"""

from __future__ import annotations

from typing import Any

from logfire.testing import CaptureLogfire

_ARTICLE_STAGE_SPAN_NAME = "article_stage"
_PIPELINE_STAGE_SPAN_NAME = "pipeline_stage"

# logfire が全 span に自動付与する framework attribute の prefix。
# - ``logfire.`` … msg / span_type / json_schema など。
# - ``code.`` … span を開いた call site の filepath / function / lineno (helper
#   自身のソース位置で固定。記事データは含まない)。
# ドメイン attribute との切り分けに使う。
_FRAMEWORK_ATTR_PREFIXES = ("logfire.", "code.")


def spans_named(capfire: CaptureLogfire, name: str) -> list[dict[str, Any]]:
    """exporter に出た指定 span 名の span を出現順に返す。"""
    return [s for s in capfire.exporter.exported_spans_as_dict() if s["name"] == name]


def one_span_named(capfire: CaptureLogfire, name: str) -> dict[str, Any]:
    """指定 span 名の span がちょうど 1 件あることを確認して返す。"""
    spans = spans_named(capfire, name)
    assert len(spans) == 1, f"expected exactly 1 {name} span, got {len(spans)}"
    return spans[0]


def article_stage_spans(capfire: CaptureLogfire) -> list[dict[str, Any]]:
    """exporter に出た ``article_stage`` span を出現順に返す。"""
    return spans_named(capfire, _ARTICLE_STAGE_SPAN_NAME)


def one_article_stage_span(capfire: CaptureLogfire) -> dict[str, Any]:
    """``article_stage`` span がちょうど 1 件あることを確認して返す。"""
    return one_span_named(capfire, _ARTICLE_STAGE_SPAN_NAME)


def stage_attrs(capfire: CaptureLogfire) -> dict[str, Any]:
    """ちょうど 1 件の ``article_stage`` span の attributes を返す。"""
    return one_article_stage_span(capfire)["attributes"]


def pipeline_stage_attrs(capfire: CaptureLogfire) -> dict[str, Any]:
    """ちょうど 1 件の ``pipeline_stage`` span の attributes を返す。"""
    return one_span_named(capfire, _PIPELINE_STAGE_SPAN_NAME)["attributes"]


def domain_attr_keys(attrs: dict[str, Any]) -> set[str]:
    """framework attribute を除いたドメイン attribute のキー集合を返す。"""
    return {k for k in attrs if not k.startswith(_FRAMEWORK_ATTR_PREFIXES)}


def exception_event(span: dict[str, Any]) -> dict[str, Any] | None:
    """span に記録された OTel ``exception`` event を返す (無ければ None)。"""
    return next((e for e in span.get("events", []) if e["name"] == "exception"), None)
