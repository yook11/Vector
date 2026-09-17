"""``app.logfire.article_stage`` helper の不変条件 (正本)。

stage span の attribute 語彙・例外 backstop・
no-override・span 文脈外 no-op・PII 非含有を、capfire の exported span を oracle に
固定する。task / service 配線テストはこの helper の上に乗るため、helper 自体の
契約はここを正本とする。

capfire は内部で ``logfire.configure(send_to_logfire=False, ...)`` を呼ぶため、
本ファイルでは ``setup_logfire`` を呼ばない。
"""

from __future__ import annotations

import asyncio

import pytest
from logfire.testing import CaptureLogfire

from app.audit.domain.event import Stage
from app.logfire.article_stage import (
    CurationStageResult,
    curation_stage_span,
    set_curation_stage_result,
)
from tests.logfire._span_helpers import domain_attr_keys, stage_attrs

# helper の signature と失敗記録 (record_failure / backstop) で載りうる
# ドメイン attribute の全集合。
# PII 防御: これ以外のキー (本文 / URL / prompt など) が span に乗らないことの oracle。
_ALLOWED_DOMAIN_KEYS = {
    "stage",
    "task_name",
    "result",
    "article_id",
    "failure_kind",
    "code",
    "retryability",
    "error_class",
    "failure_action",
}


# 不変条件 1: open 時の attribute


def test_curation_open_attributes(capfire: CaptureLogfire) -> None:
    """Curation開始時に工程・タスク名・記事IDを記録する。"""
    with curation_stage_span(article_id=7):
        pass
    attrs = stage_attrs(capfire)
    assert attrs["stage"] == Stage.CURATION.value
    assert attrs["task_name"] == "curate_content"
    assert attrs["article_id"] == 7


# 不変条件 2: set_result が各語彙を反映 (handle 経由 = task が使う API)

_CURATION_RESULTS: list[CurationStageResult] = [
    "signal",
    "noise",
    "skipped",
    "failed",
]


@pytest.mark.parametrize("result", _CURATION_RESULTS)
def test_curation_set_result_reflects(
    capfire: CaptureLogfire, result: CurationStageResult
) -> None:
    """curation handle.set_result が span に result を反映する。"""
    with curation_stage_span(article_id=1) as stage:
        stage.set_result(result)
    assert stage_attrs(capfire)["result"] == result


# 不変条件 2': module 関数経由 = service が使う API。span 内なら result を焼く。


def test_curation_module_function_sets_result_inside_span(
    capfire: CaptureLogfire,
) -> None:
    """``set_curation_stage_result`` が現在の curation span に result を焼く。"""
    with curation_stage_span(article_id=1):
        set_curation_stage_result("signal")
    assert stage_attrs(capfire)["result"] == "signal"


# 不変条件 6: 例外 backstop (result 未設定で例外貫通 → failed + 再送出)


def test_exception_without_result_sets_failed_and_propagates(
    capfire: CaptureLogfire,
) -> None:
    """with 内で raise かつ result 未設定なら result=failed で閉じ、例外は伝搬する。"""
    with pytest.raises(ValueError, match="boom"):
        with curation_stage_span(article_id=1):
            raise ValueError("boom")
    assert stage_attrs(capfire)["result"] == "failed"


# 不変条件 7: result 設定済みなら例外時も上書きしない


def test_exception_after_result_does_not_override(capfire: CaptureLogfire) -> None:
    """result 設定後に raise しても backstop は result を上書きしない。

    例: signal 保存 commit 済みの後に kiq が落ちた場合、result=signal のまま残す。
    """
    with pytest.raises(RuntimeError, match="kiq down"):
        with curation_stage_span(article_id=1) as stage:
            stage.set_result("signal")
            raise RuntimeError("kiq down")
    assert stage_attrs(capfire)["result"] == "signal"


# 不変条件 7b: record_failure / backstop が failure projection 由来の分類属性を焼く


def test_record_failure_copies_projection_attributes(capfire: CaptureLogfire) -> None:
    """record_failure は project_failure の値 + error_class を span に焼く。"""
    with curation_stage_span(article_id=1) as stage:
        stage.set_result("failed")
        stage.record_failure(ValueError("boom"))
    attrs = stage_attrs(capfire)
    assert attrs["failure_kind"] == "unknown"
    assert attrs["code"] == "unexpected_error"
    assert attrs["retryability"] == "unknown"
    assert attrs["error_class"] == "builtins.ValueError"
    # drop_article でない catch-all は failure_action を載せない (条件付き属性)。
    assert "failure_action" not in attrs


def test_record_failure_is_no_override(capfire: CaptureLogfire) -> None:
    """record_failure は一度だけ焼く。二次例外で呼ばれても元の分類を上書きしない。"""
    with curation_stage_span(article_id=1) as stage:
        stage.record_failure(ValueError("original business error"))
        stage.record_failure(RuntimeError("secondary audit/hold error"))
    assert stage_attrs(capfire)["error_class"] == "builtins.ValueError"


def test_backstop_records_failure_attributes_on_propagating_exception(
    capfire: CaptureLogfire,
) -> None:
    """明示 record 無しで貫通した Exception は backstop が分類属性を焼く。"""
    with pytest.raises(ValueError, match="boom"):
        with curation_stage_span(article_id=1):
            raise ValueError("boom")
    attrs = stage_attrs(capfire)
    assert attrs["result"] == "failed"
    assert attrs["failure_kind"] == "unknown"
    assert attrs["error_class"] == "builtins.ValueError"


def test_cancellation_sets_failed_but_no_failure_attributes(
    capfire: CaptureLogfire,
) -> None:
    """CancelledError は result=failed (既存挙動) だが失敗分類属性は載せない。"""
    with pytest.raises(asyncio.CancelledError):
        with curation_stage_span(article_id=1):
            raise asyncio.CancelledError
    attrs = stage_attrs(capfire)
    assert attrs["result"] == "failed"
    assert "failure_kind" not in attrs
    assert "error_class" not in attrs


# 不変条件 8: span 文脈外で module 関数は no-op (例外を投げない / span を作らない)


def test_module_functions_noop_outside_span(capfire: CaptureLogfire) -> None:
    """span 外 (CLI / service 単体) で result setter を呼んでも落ちず span も出ない。"""
    set_curation_stage_result("signal")
    assert capfire.exporter.exported_spans_as_dict() == []


# 不変条件 9: PII — ドメイン attribute は許可キーのみ (本文 / URL / prompt は乗らない)


def test_no_unexpected_attributes_curation(capfire: CaptureLogfire) -> None:
    """curation span のドメイン attribute は許可キー集合の部分集合に収まる。"""
    with curation_stage_span(article_id=1) as stage:
        stage.set_result("signal")
    keys = domain_attr_keys(stage_attrs(capfire))
    assert keys <= _ALLOWED_DOMAIN_KEYS, f"unexpected attribute keys: {keys}"


def test_no_unexpected_attributes_on_failure_path(capfire: CaptureLogfire) -> None:
    """貫通例外で失敗分類属性を焼いても、span 属性は許可キー集合内 (属性チャネルのみ)。

    検証範囲は span の **属性チャネル** のみ。例外 message / stacktrace は別の OTel
    exception event チャネルに入り (本テストの対象外・未 redact)、属性へは昇格しない。
    """
    with pytest.raises(ValueError):
        with curation_stage_span(article_id=1):
            raise ValueError("token=sk-secret https://internal/secret?q=1")
    keys = domain_attr_keys(stage_attrs(capfire))
    assert keys <= _ALLOWED_DOMAIN_KEYS, f"unexpected attribute keys: {keys}"
