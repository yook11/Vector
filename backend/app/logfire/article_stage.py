"""CurationとAssessmentのTaskiq実行に記事単位のspanを付与する。"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal

import logfire
from logfire import LogfireSpan

from app.audit.domain.event import Stage
from app.logfire.failure_attrs import annotate_span_failure

# stage 別の result 語彙。値だけで「記事がどう抜けたか」が読めるよう自己記述的にする。
CurationStageResult = Literal["signal", "noise", "skipped", "failed"]
AssessmentResult = Literal["in_scope", "out_of_scope", "skipped", "failed"]

_SPAN_NAME = "article_stage"


class CurationStageSpan:
    """curation task の記録口。article_id は open 時に確定、次工程は assess_content。"""

    def __init__(self, span: LogfireSpan) -> None:
        self._span = span
        self._result_set = False
        self._failure_set = False

    def set_result(self, result: CurationStageResult) -> None:
        """result を一度だけ焼く (no-override)。"""
        if self._result_set:
            return
        self._span.set_attribute("result", result)
        self._result_set = True

    def record_failure(self, exc: Exception) -> None:
        """失敗分類属性を一度だけ焼く (no-override)。元の業務例外を最優先で残す。"""
        if self._failure_set:
            return
        annotate_span_failure(self._span, exc)
        self._failure_set = True

    def mark_next_task_enqueued(self) -> None:
        """assess_content の kiq 成功直後に呼ぶ。enqueued フラグと次 task 名を焼く。"""
        self._span.set_attribute("next_task_enqueued", True)
        self._span.set_attribute("next_task_name", "assess_content")


class AssessmentStageSpan:
    """assessment task の記録口。article_id は ready 構築後に late-bind する。"""

    def __init__(self, span: LogfireSpan) -> None:
        self._span = span
        self._result_set = False
        self._failure_set = False

    def set_result(self, result: AssessmentResult) -> None:
        """result を一度だけ焼く (no-override)。"""
        if self._result_set:
            return
        self._span.set_attribute("result", result)
        self._result_set = True

    def record_failure(self, exc: Exception) -> None:
        """失敗分類属性を一度だけ焼く (no-override)。元の業務例外を最優先で残す。"""
        if self._failure_set:
            return
        annotate_span_failure(self._span, exc)
        self._failure_set = True

    def set_article_id(self, article_id: int) -> None:
        """trigger に無く ready で判明する article_id を後付けする。"""
        self._span.set_attribute("article_id", article_id)


_current_stage_span: contextvars.ContextVar[
    CurationStageSpan | AssessmentStageSpan | None
] = contextvars.ContextVar("article_stage_span", default=None)


@contextmanager
def curation_stage_span(*, article_id: int) -> Iterator[CurationStageSpan]:
    """curation task の ``article_stage`` span を開く context manager。

    open 時は ``next_task_enqueued=False`` のみ載せ、``next_task_name`` は載せない
    (kiq 成功後に ``mark_next_task_enqueued`` が同時に焼く)。例外貫通かつ result
    未設定なら backstop で ``failed`` を焼いてから例外を再送出する。
    """
    with logfire.span(
        _SPAN_NAME,
        stage=Stage.CURATION.value,
        task_name="curate_content",
        article_id=article_id,
        next_task_enqueued=False,
    ) as span:
        recorder = CurationStageSpan(span)
        token = _current_stage_span.set(recorder)
        try:
            yield recorder
        except BaseException as exc:
            recorder.set_result("failed")
            if isinstance(exc, Exception):
                recorder.record_failure(exc)
            raise
        finally:
            _current_stage_span.reset(token)


@contextmanager
def assessment_stage_span(*, curation_id: int) -> Iterator[AssessmentStageSpan]:
    """assessment task の ``article_stage`` span を開く context manager。

    article_id は trigger に無いため open 時には載せず、ready 構築後に
    ``set_article_id`` で後付けする。backstop / contextvar の扱いは curation と同じ。
    """
    with logfire.span(
        _SPAN_NAME,
        stage=Stage.ASSESSMENT.value,
        task_name="assess_content",
        curation_id=curation_id,
        next_task_enqueued=False,
    ) as span:
        recorder = AssessmentStageSpan(span)
        token = _current_stage_span.set(recorder)
        try:
            yield recorder
        except BaseException as exc:
            recorder.set_result("failed")
            if isinstance(exc, Exception):
                recorder.record_failure(exc)
            raise
        finally:
            _current_stage_span.reset(token)


def set_curation_stage_result(result: CurationStageResult) -> None:
    """現在の span が curation の時だけ result を焼く。それ以外は no-op。

    span 文脈外 (CLI / service 単体テスト) でも、別ステージ span の最中の誤呼び出し
    でも、recorder が curation 型でなければ何もしない (別ステージ span への誤焼き
    防止)。setter は自ステージの recorder にしか効かない。
    """
    recorder = _current_stage_span.get()
    if not isinstance(recorder, CurationStageSpan):
        return
    recorder.set_result(result)


def set_assessment_stage_result(result: AssessmentResult) -> None:
    """現在の span が assessment の時だけ result を焼く。それ以外は no-op。"""
    recorder = _current_stage_span.get()
    if not isinstance(recorder, AssessmentStageSpan):
        return
    recorder.set_result(result)
