"""``app.logfire.article_stage`` helper の不変条件 (正本)。

stage span の attribute 語彙・mark の意味・終端ステージの構造保証・例外 backstop・
no-override・span 文脈外 no-op・PII 非含有を、capfire の exported span を oracle に
固定する。task / service 配線テストはこの helper の上に乗るため、helper 自体の
契約はここを正本とする。

capfire は内部で ``logfire.configure(send_to_logfire=False, ...)`` を呼ぶため、
本ファイルでは ``setup_logfire`` を呼ばない。
"""

from __future__ import annotations

import pytest
from logfire.testing import CaptureLogfire

from app.logfire.article_stage import (
    AssessmentResult,
    CurationResult,
    EmbeddingResult,
    EmbeddingStageSpan,
    assessment_stage_span,
    curation_stage_span,
    embedding_stage_span,
    set_assessment_stage_result,
    set_curation_stage_result,
    set_embedding_stage_result,
)
from tests.logfire._span_helpers import domain_attr_keys, stage_attrs

# helper の signature だけで載りうるドメイン attribute の全集合。
# PII 防御: これ以外のキー (本文 / URL / prompt など) が span に乗らないことの oracle。
_ALLOWED_DOMAIN_KEYS = {
    "stage",
    "task_name",
    "result",
    "article_id",
    "curation_id",
    "analyzed_article_id",
    "next_task_enqueued",
    "next_task_name",
}


# 不変条件 1: open 時の attribute


def test_curation_open_attributes(capfire: CaptureLogfire) -> None:
    """curation open で stage / task_name / article_id / next_task_enqueued=False。

    next_task_name は open 時には載らない (kiq 成功後にだけ載る)。
    """
    with curation_stage_span(article_id=7):
        pass
    attrs = stage_attrs(capfire)
    assert attrs["stage"] == "curation"
    assert attrs["task_name"] == "curate_content"
    assert attrs["article_id"] == 7
    assert attrs["next_task_enqueued"] is False
    assert "next_task_name" not in attrs


def test_assessment_open_attributes(capfire: CaptureLogfire) -> None:
    """assessment open で stage / task_name / curation_id / next_task_enqueued=False。

    article_id は open 時には無く (ready で late-bind)、next_task_name も無い。
    """
    with assessment_stage_span(curation_id=11):
        pass
    attrs = stage_attrs(capfire)
    assert attrs["stage"] == "assessment"
    assert attrs["task_name"] == "assess_content"
    assert attrs["curation_id"] == 11
    assert attrs["next_task_enqueued"] is False
    assert "article_id" not in attrs
    assert "next_task_name" not in attrs


def test_embedding_open_attributes(capfire: CaptureLogfire) -> None:
    """embedding open では analyzed_article_id を持ち、next_task 系は無い。"""
    with embedding_stage_span(analyzed_article_id=13):
        pass
    attrs = stage_attrs(capfire)
    assert attrs["stage"] == "embedding"
    assert attrs["task_name"] == "generate_embedding"
    assert attrs["analyzed_article_id"] == 13
    assert "analysis_id" not in attrs
    assert "next_task_enqueued" not in attrs
    assert "next_task_name" not in attrs
    assert "article_id" not in attrs


def test_embedding_stage_span_rejects_legacy_analysis_id_keyword() -> None:
    with pytest.raises(TypeError):
        with embedding_stage_span(analysis_id=13):
            pass


# 不変条件 2: set_result が各語彙を反映 (handle 経由 = task が使う API)

_CURATION_RESULTS: list[CurationResult] = [
    "signal",
    "noise",
    "rate_limited",
    "skipped",
    "failed",
]
_ASSESSMENT_RESULTS: list[AssessmentResult] = [
    "in_scope",
    "out_of_scope",
    "rate_limited",
    "skipped",
    "failed",
]
_EMBEDDING_RESULTS: list[EmbeddingResult] = [
    "succeeded",
    "rate_limited",
    "skipped",
    "failed",
]


@pytest.mark.parametrize("result", _CURATION_RESULTS)
def test_curation_set_result_reflects(
    capfire: CaptureLogfire, result: CurationResult
) -> None:
    """curation handle.set_result が span に result を反映する。"""
    with curation_stage_span(article_id=1) as stage:
        stage.set_result(result)
    assert stage_attrs(capfire)["result"] == result


@pytest.mark.parametrize("result", _ASSESSMENT_RESULTS)
def test_assessment_set_result_reflects(
    capfire: CaptureLogfire, result: AssessmentResult
) -> None:
    """assessment handle.set_result が span に result を反映する。"""
    with assessment_stage_span(curation_id=1) as stage:
        stage.set_result(result)
    assert stage_attrs(capfire)["result"] == result


@pytest.mark.parametrize("result", _EMBEDDING_RESULTS)
def test_embedding_set_result_reflects(
    capfire: CaptureLogfire, result: EmbeddingResult
) -> None:
    """embedding handle.set_result が span に result を反映する。"""
    with embedding_stage_span(analyzed_article_id=1) as stage:
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


def test_assessment_module_function_sets_result_inside_span(
    capfire: CaptureLogfire,
) -> None:
    """``set_assessment_stage_result`` が現在の assessment span に result を焼く。"""
    with assessment_stage_span(curation_id=1):
        set_assessment_stage_result("in_scope")
    assert stage_attrs(capfire)["result"] == "in_scope"


def test_embedding_module_function_sets_result_inside_span(
    capfire: CaptureLogfire,
) -> None:
    """``set_embedding_stage_result`` が現在の embedding span に result を焼く。"""
    with embedding_stage_span(analyzed_article_id=1):
        set_embedding_stage_result("succeeded")
    assert stage_attrs(capfire)["result"] == "succeeded"


# 不変条件 3: set_article_id の late-binding (assessment / embedding)


def test_assessment_set_article_id_late_binds(capfire: CaptureLogfire) -> None:
    """assessment は open 後に set_article_id で article_id を後付けできる。"""
    with assessment_stage_span(curation_id=11) as stage:
        stage.set_article_id(99)
    assert stage_attrs(capfire)["article_id"] == 99


def test_embedding_set_article_id_late_binds(capfire: CaptureLogfire) -> None:
    """embedding は open 後に set_article_id で article_id を後付けできる。"""
    with embedding_stage_span(analyzed_article_id=13) as stage:
        stage.set_article_id(99)
    assert stage_attrs(capfire)["article_id"] == 99


# 不変条件 4: mark_next_task_enqueued の意味 (kiq 成功後にだけ次 task 名が載る)


def test_curation_mark_next_task_sets_flag_and_name(
    capfire: CaptureLogfire,
) -> None:
    """curation の mark 後に next_task_enqueued=True と name=assess_content が載る。"""
    with curation_stage_span(article_id=1) as stage:
        stage.mark_next_task_enqueued()
    attrs = stage_attrs(capfire)
    assert attrs["next_task_enqueued"] is True
    assert attrs["next_task_name"] == "assess_content"


def test_assessment_mark_next_task_sets_flag_and_name(
    capfire: CaptureLogfire,
) -> None:
    """assessment の mark 後に next_task_enqueued=True + name=generate_embedding。"""
    with assessment_stage_span(curation_id=1) as stage:
        stage.mark_next_task_enqueued()
    attrs = stage_attrs(capfire)
    assert attrs["next_task_enqueued"] is True
    assert attrs["next_task_name"] == "generate_embedding"


def test_curation_without_mark_has_no_next_task_name(
    capfire: CaptureLogfire,
) -> None:
    """mark しない経路 (例 noise) では next_task_name が出ず enqueued は False。"""
    with curation_stage_span(article_id=1) as stage:
        stage.set_result("noise")
    attrs = stage_attrs(capfire)
    assert attrs["next_task_enqueued"] is False
    assert "next_task_name" not in attrs


def test_assessment_without_mark_has_no_next_task_name(
    capfire: CaptureLogfire,
) -> None:
    """mark しない経路 (例: out_of_scope) では next_task_name が出ない。"""
    with assessment_stage_span(curation_id=1) as stage:
        stage.set_result("out_of_scope")
    attrs = stage_attrs(capfire)
    assert attrs["next_task_enqueued"] is False
    assert "next_task_name" not in attrs


# 不変条件 5: 終端ステージ embedding は mark_next_task_enqueued を持たない


def test_embedding_handle_has_no_mark_next_task() -> None:
    """embedding handle に mark_next_task_enqueued が無い (終端ステージの構造保証)。"""
    assert not hasattr(EmbeddingStageSpan, "mark_next_task_enqueued")


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


# 不変条件 8: span 文脈外で module 関数は no-op (例外を投げない / span を作らない)


def test_module_functions_noop_outside_span(capfire: CaptureLogfire) -> None:
    """span 外 (CLI / service 単体) で result setter を呼んでも落ちず span も出ない。"""
    set_curation_stage_result("signal")
    set_assessment_stage_result("in_scope")
    set_embedding_stage_result("succeeded")
    assert capfire.exporter.exported_spans_as_dict() == []


# 不変条件 8': setter は自ステージの recorder にしか効かない (cross-stage 誤焼き防止)


def test_cross_stage_setter_is_noop(capfire: CaptureLogfire) -> None:
    """別ステージの setter は現在の span に効かない。

    assessment span の最中に curation の setter を呼んでも、recorder が curation 型
    でないため result は焼かれない (assessment span に誤って signal が乗らない)。
    """
    with assessment_stage_span(curation_id=1):
        set_curation_stage_result("signal")
    assert "result" not in stage_attrs(capfire)


# 不変条件 9: PII — ドメイン attribute は許可キーのみ (本文 / URL / prompt は乗らない)


def test_no_unexpected_attributes_curation(capfire: CaptureLogfire) -> None:
    """curation span のドメイン attribute は許可キー集合の部分集合に収まる。"""
    with curation_stage_span(article_id=1) as stage:
        stage.set_result("signal")
        stage.mark_next_task_enqueued()
    keys = domain_attr_keys(stage_attrs(capfire))
    assert keys <= _ALLOWED_DOMAIN_KEYS, f"unexpected attribute keys: {keys}"


def test_no_unexpected_attributes_assessment(capfire: CaptureLogfire) -> None:
    """assessment span のドメイン attribute は許可キー集合の部分集合に収まる。"""
    with assessment_stage_span(curation_id=1) as stage:
        stage.set_article_id(2)
        stage.set_result("in_scope")
        stage.mark_next_task_enqueued()
    keys = domain_attr_keys(stage_attrs(capfire))
    assert keys <= _ALLOWED_DOMAIN_KEYS, f"unexpected attribute keys: {keys}"


def test_no_unexpected_attributes_embedding(capfire: CaptureLogfire) -> None:
    """embedding span のドメイン attribute は許可キー集合の部分集合に収まる。"""
    with embedding_stage_span(analyzed_article_id=1) as stage:
        stage.set_article_id(2)
        stage.set_result("succeeded")
    keys = domain_attr_keys(stage_attrs(capfire))
    assert keys <= _ALLOWED_DOMAIN_KEYS, f"unexpected attribute keys: {keys}"
