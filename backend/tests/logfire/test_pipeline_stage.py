"""``app.logfire.stage_span.pipeline_stage_span`` helper の不変条件 (正本)。

非 AI worker 工程の span 契約 — span_name 固定 / stage 属性が Stage の wire 値 /
op と任意 ID の attribute / 持たない ID は載らない / 例外貫通 / PII 非含有 — を
capfire の exported span を oracle に固定する。task 配線テストはこの helper の上に
乗るため、helper 自体の契約はここを正本とする。

capfire は内部で ``logfire.configure(send_to_logfire=False, ...)`` を呼ぶため、
本ファイルでは ``setup_logfire`` を呼ばない。
"""

from __future__ import annotations

import pytest
from logfire.testing import CaptureLogfire

from app.audit.domain.event import Stage
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

# helper の signature だけで載りうるドメイン attribute の全集合。
# PII 防御: これ以外のキー (本文 / URL / prompt など) が span に乗らないことの oracle。
# PR2 で failure_kind / code / error_class / retryability を追加する。
_ALLOWED_DOMAIN_KEYS = {"stage", "op", "source_id", "article_id"}


# 不変条件 1: span_name は固定。識別子は attribute へ分離する。


def test_span_name_is_pipeline_stage(capfire: CaptureLogfire) -> None:
    """span は ``pipeline_stage`` 名でちょうど 1 件出る (低 cardinality 固定)。"""
    with pipeline_stage_span(Stage.TREND_DISCOVERY, op="run_trend_discovery"):
        pass
    one_span_named(capfire, _SPAN_NAME)


# 不変条件 2: stage 属性は Stage の wire 値 (enum object でなく str)


def test_stage_attribute_is_enum_wire_value(capfire: CaptureLogfire) -> None:
    """stage 属性は ``Stage.ACQUISITION.value`` = "acquisition" (DB CHECK wire 値)。"""
    with pipeline_stage_span(Stage.ACQUISITION, op="acquire_source", source_id=1):
        pass
    attrs = pipeline_stage_attrs(capfire)
    # stage は DB CHECK と一致する wire 値で載る (enum repr でない)。
    assert attrs["stage"] == Stage.ACQUISITION.value
    assert attrs["stage"] == "acquisition"


# 不変条件 3: source_id を持つ工程は stage / op / source_id を載せ article_id は載せない


def test_source_scoped_attributes(capfire: CaptureLogfire) -> None:
    """acquisition 形 (source_id あり / article_id なし)。"""
    with pipeline_stage_span(Stage.ACQUISITION, op="acquire_source", source_id=42):
        pass
    attrs = pipeline_stage_attrs(capfire)
    assert attrs["op"] == "acquire_source"
    assert attrs["source_id"] == 42
    assert "article_id" not in attrs


# 不変条件 4: article_id を持つ工程は article_id を載せ source_id は載せない


def test_article_scoped_attributes(capfire: CaptureLogfire) -> None:
    """completion 形 (article_id あり / source_id なし)。"""
    with pipeline_stage_span(Stage.COMPLETION, op="scrape_html_body", article_id=7):
        pass
    attrs = pipeline_stage_attrs(capfire)
    assert attrs["op"] == "scrape_html_body"
    assert attrs["article_id"] == 7
    assert "source_id" not in attrs


# 不変条件 5: run 単位工程 (ID なし) は stage / op のみ。ID attribute は載らない。


@pytest.mark.parametrize(
    ("stage", "op"),
    [
        (Stage.BRIEFING, "generate_briefing_for_category"),
        (Stage.TREND_DISCOVERY, "run_trend_discovery"),
        (Stage.BACKFILL_CURATE, "backfill_curations"),
        (Stage.BACKFILL_ASSESS, "backfill_assessments"),
        (Stage.BACKFILL_EMBED, "backfill_embeddings"),
    ],
)
def test_run_scoped_attributes(capfire: CaptureLogfire, stage: Stage, op: str) -> None:
    """ID を持たない run 単位工程は stage / op のみで、ID attribute は載らない。"""
    with pipeline_stage_span(stage, op=op):
        pass
    attrs = pipeline_stage_attrs(capfire)
    assert attrs["stage"] == stage.value
    assert attrs["op"] == op
    assert "source_id" not in attrs
    assert "article_id" not in attrs


# 不変条件 6: 例外は握り潰さず貫通し、span に exception event として記録される


def test_exception_propagates_and_records_type(capfire: CaptureLogfire) -> None:
    """span 内 raise は貫通し、例外型が OTel exception event に乗る。"""
    with pytest.raises(ValueError, match="boom"):
        with pipeline_stage_span(Stage.ACQUISITION, op="acquire_source", source_id=1):
            raise ValueError("boom")
    event = exception_event(one_span_named(capfire, _SPAN_NAME))
    assert event is not None and event["attributes"]["exception.type"] == "ValueError"


# 不変条件 7: 貫通例外は span level を error へ自動昇格する (doc I5)


def test_exception_escalates_level_to_error(capfire: CaptureLogfire) -> None:
    """span 内 raise で span の logfire level が error (17) になる。"""
    with pytest.raises(ValueError, match="boom"):
        with pipeline_stage_span(Stage.ACQUISITION, op="acquire_source", source_id=1):
            raise ValueError("boom")
    span = one_span_named(capfire, _SPAN_NAME)
    assert span["attributes"]["logfire.level_num"] == _LEVEL_ERROR


# 不変条件 8: PII — ドメイン attribute は許可キーのみ (本文 / URL / prompt は乗らない)


def test_no_unexpected_attributes(capfire: CaptureLogfire) -> None:
    """source_id / article_id を両方渡しても、ドメイン attribute は許可キー集合内。"""
    with pipeline_stage_span(
        Stage.COMPLETION, op="scrape_html_body", source_id=1, article_id=2
    ):
        pass
    keys = domain_attr_keys(pipeline_stage_attrs(capfire))
    assert keys <= _ALLOWED_DOMAIN_KEYS, f"unexpected attribute keys: {keys}"
