"""``app.logfire.stage_span.pipeline_stage_span`` helper の不変条件 (正本)。

非 AI worker 工程の span 契約 — span_name 固定 / stage 属性は渡した文字列 /
op と任意 ID の attribute / 持たない ID は載らない / 例外貫通 / PII 非含有 — を
capfire の exported span を oracle に固定する。task 配線テストはこの helper の上に
乗るため、helper 自体の契約はここを正本とする。

capfire は内部で ``logfire.configure(send_to_logfire=False, ...)`` を呼ぶため、
本ファイルでは ``setup_logfire`` を呼ばない。
"""

from __future__ import annotations

import asyncio

import pytest
from logfire.testing import CaptureLogfire

from app.insights.briefing.errors import BriefingConfigurationError
from app.logfire.stage_span import pipeline_stage_span
from tests.logfire._span_helpers import (
    domain_attr_keys,
    exception_event,
    one_span_named,
    pipeline_stage_attrs,
)

_SPAN_NAME = "pipeline_stage"

# logfire の level スケール: trace=1 / debug=5 / info=9 / notice=10 / warn=13 /
# error=17 / fatal=21。貫通例外で span は error へ自動昇格する (doc I5)。
_LEVEL_ERROR = 17

# helper の signature と失敗 backstop で載りうるドメイン attribute の全集合。
# PII 防御: これ以外のキー (本文 / URL / prompt など) が span に乗らないことの oracle。
_ALLOWED_DOMAIN_KEYS = {
    "stage",
    "op",
    "source_id",
    "article_id",
    "error_class",
}


# 不変条件 1: span_name は固定。識別子は attribute へ分離する。


def test_span_name_is_pipeline_stage(capfire: CaptureLogfire) -> None:
    """span は ``pipeline_stage`` 名でちょうど 1 件出る (低 cardinality 固定)。"""
    with pipeline_stage_span("trend_discovery", op="run_trend_discovery"):
        pass
    one_span_named(capfire, _SPAN_NAME)


# 不変条件 2: stage 属性は渡した文字列のまま載る


def test_stage_attribute_is_passed_string(capfire: CaptureLogfire) -> None:
    """stage 属性は呼び出し元が渡した文字列のまま載る。"""
    with pipeline_stage_span("acquisition", op="acquire_source", source_id=1):
        pass
    attrs = pipeline_stage_attrs(capfire)
    assert attrs["stage"] == "acquisition"


# 不変条件 3: source_id を持つ工程は stage / op / source_id を載せ article_id は載せない


def test_source_scoped_attributes(capfire: CaptureLogfire) -> None:
    """acquisition 形 (source_id あり / article_id なし)。"""
    with pipeline_stage_span("acquisition", op="acquire_source", source_id=42):
        pass
    attrs = pipeline_stage_attrs(capfire)
    assert attrs["op"] == "acquire_source"
    assert attrs["source_id"] == 42
    assert "article_id" not in attrs


# 不変条件 4: article_id を持つ工程は article_id を載せ source_id は載せない


def test_article_scoped_attributes(capfire: CaptureLogfire) -> None:
    """completion 形 (article_id あり / source_id なし)。"""
    with pipeline_stage_span("completion", op="scrape_html_body", article_id=7):
        pass
    attrs = pipeline_stage_attrs(capfire)
    assert attrs["op"] == "scrape_html_body"
    assert attrs["article_id"] == 7
    assert "source_id" not in attrs


# 不変条件 5: run 単位工程 (ID なし) は stage / op のみ。ID attribute は載らない。


@pytest.mark.parametrize(
    ("stage", "op"),
    [
        ("briefing", "generate_briefing_for_category"),
        ("trend_discovery", "run_trend_discovery"),
    ],
)
def test_run_scoped_attributes(capfire: CaptureLogfire, stage: str, op: str) -> None:
    """ID を持たない run 単位工程は stage / op のみで、ID attribute は載らない。"""
    with pipeline_stage_span(stage, op=op):
        pass
    attrs = pipeline_stage_attrs(capfire)
    assert attrs["stage"] == stage
    assert attrs["op"] == op
    assert "source_id" not in attrs
    assert "article_id" not in attrs


# 不変条件 6: 例外は握り潰さず貫通し、span に exception event として記録される


def test_exception_propagates_and_records_type(capfire: CaptureLogfire) -> None:
    """span 内 raise は貫通し、例外型が OTel exception event に乗る。"""
    with pytest.raises(ValueError, match="boom"):
        with pipeline_stage_span("acquisition", op="acquire_source", source_id=1):
            raise ValueError("boom")
    event = exception_event(one_span_named(capfire, _SPAN_NAME))
    assert event is not None and event["attributes"]["exception.type"] == "ValueError"


# 不変条件 7: 貫通例外は span level を error へ自動昇格する (doc I5)


def test_exception_escalates_level_to_error(capfire: CaptureLogfire) -> None:
    """span 内 raise で span の logfire level が error (17) になる。"""
    with pytest.raises(ValueError, match="boom"):
        with pipeline_stage_span("acquisition", op="acquire_source", source_id=1):
            raise ValueError("boom")
    span = one_span_named(capfire, _SPAN_NAME)
    assert span["attributes"]["logfire.level_num"] == _LEVEL_ERROR


# 不変条件 7b: 貫通例外は例外型 (error_class) を span の属性に焼く


def test_exception_records_error_class(capfire: CaptureLogfire) -> None:
    """貫通例外の完全修飾の型名が span の ``error_class`` に載る。"""
    with pytest.raises(ValueError, match="boom"):
        with pipeline_stage_span("acquisition", op="acquire_source", source_id=1):
            raise ValueError("boom")
    attrs = pipeline_stage_attrs(capfire)
    assert attrs["error_class"] == "builtins.ValueError"


# 不変条件 7c: 協調キャンセルは失敗ではない (error_class を載せない)


def test_cancellation_does_not_record_error_class(
    capfire: CaptureLogfire,
) -> None:
    """CancelledError は ``except Exception`` を素通りし error_class を載せない。"""
    with pytest.raises(asyncio.CancelledError):
        with pipeline_stage_span("acquisition", op="acquire_source", source_id=1):
            raise asyncio.CancelledError
    attrs = pipeline_stage_attrs(capfire)
    assert "error_class" not in attrs


# 不変条件 7d: 明示 record_failure (握り潰し経路) と no-override


def test_record_failure_via_recorder_sets_error_class(
    capfire: CaptureLogfire,
) -> None:
    """握り潰し経路: raise せず ``record_failure`` を呼ぶと error_class が載る。"""
    exc = BriefingConfigurationError("missing key")
    with pipeline_stage_span("acquisition", op="acquire_source", source_id=1) as stage:
        stage.record_failure(exc)
    attrs = pipeline_stage_attrs(capfire)
    assert attrs["error_class"].endswith(".BriefingConfigurationError")


def test_record_failure_is_no_override(capfire: CaptureLogfire) -> None:
    """record_failure は一度だけ焼き、二度目の例外で error_class を上書きしない。"""
    first = BriefingConfigurationError("missing key")
    with pipeline_stage_span("acquisition", op="acquire_source", source_id=1) as stage:
        stage.record_failure(first)
        stage.record_failure(ValueError("secondary"))
    attrs = pipeline_stage_attrs(capfire)
    assert attrs["error_class"].endswith(".BriefingConfigurationError")


def test_explicit_record_then_propagating_secondary_keeps_original(
    capfire: CaptureLogfire,
) -> None:
    """明示記録後に別例外が貫通しても、backstop は元の error_class を上書きしない。

    二次例外を模す: 先に業務例外を記録し、後から別例外が span を貫通しても
    span の error_class は最初の業務例外のまま。
    """
    business = BriefingConfigurationError("missing key")
    with pytest.raises(RuntimeError, match="secondary failure"):
        with pipeline_stage_span(
            "acquisition", op="acquire_source", source_id=1
        ) as stage:
            stage.record_failure(business)
            raise RuntimeError("secondary failure")
    attrs = pipeline_stage_attrs(capfire)
    assert attrs["error_class"].endswith(".BriefingConfigurationError")


# 不変条件 8: PII — ドメイン attribute は許可キーのみ (本文 / URL / prompt は乗らない)


def test_no_unexpected_attributes(capfire: CaptureLogfire) -> None:
    """source_id / article_id を両方渡しても、ドメイン attribute は許可キー集合内。"""
    with pipeline_stage_span(
        "completion", op="scrape_html_body", source_id=1, article_id=2
    ):
        pass
    keys = domain_attr_keys(pipeline_stage_attrs(capfire))
    assert keys <= _ALLOWED_DOMAIN_KEYS, f"unexpected attribute keys: {keys}"


def test_no_unexpected_attributes_on_failure_path(capfire: CaptureLogfire) -> None:
    """失敗 backstop は許可キー以外を span 属性に焼かない (属性チャネルのみの oracle)。

    検証範囲は span の **属性チャネル** のみ。例外 message / stacktrace は別の OTel
    exception event チャネルに入り (本テストの対象外・未 redact)、属性へは昇格しない。
    機微文字列を含む例外でも、属性キーが許可集合内に留まることだけを固定する。
    """
    with pytest.raises(ValueError):
        with pipeline_stage_span("acquisition", op="acquire_source", source_id=1):
            raise ValueError("token=sk-secret https://internal/secret?q=1")
    keys = domain_attr_keys(pipeline_stage_attrs(capfire))
    assert keys <= _ALLOWED_DOMAIN_KEYS, f"unexpected attribute keys: {keys}"
