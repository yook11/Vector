"""CurationのTaskiq実行に記事単位のspanを付与する。"""

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

_SPAN_NAME = "article_stage"


class CurationStageSpan:
    """curation task の記事と処理結果を記録する。"""

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


_current_stage_span: contextvars.ContextVar[CurationStageSpan | None] = (
    contextvars.ContextVar("article_stage_span", default=None)
)


@contextmanager
def curation_stage_span(*, article_id: int) -> Iterator[CurationStageSpan]:
    """Curationのspanを開き、未記録のまま例外が伝播した場合は失敗を記録する。"""
    with logfire.span(
        _SPAN_NAME,
        stage=Stage.CURATION.value,
        task_name="curate_content",
        article_id=article_id,
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
