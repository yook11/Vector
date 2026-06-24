"""非 AI worker 工程の Logfire span helper。

``article_stage`` (AI 分析 3 工程) が持つ result 語彙を非 AI 工程は持たないため、
``Stage`` を強制注入する最小 recorder を yield する context manager を 1 本置く。
recorder は result 語彙を持たず、失敗分類の複写 (``record_failure``) だけを担う。span は
taskiq ``OpenTelemetryMiddleware`` の ``execute/<task_name>`` span の子として開き、
「どの工程で・どの場所で例外が起きたか」を Logfire の stage 軸で絞り込めるようにする。

span attribute には本文・URL・prompt・AI response・認証情報を載せない。低 cardinality
の語彙 (stage / op) と内部 DB ID (source_id / article_id) のみを載せる。span 内を
貫通した例外は ``logfire.span`` が OTel exception event として自動記録し level を error
に上げる。加えて backstop で failure projection 由来の失敗分類属性 (failure_kind /
code / retryability / error_class) も焼く。協調キャンセル (CancelledError 等) は
``except Exception`` で失敗軸から除外する。再 raise せず握り潰す失敗 (acquire_source の
非 retryable 経路など) は呼び出し側が ``record_failure`` を明示する (no-override)。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import logfire
from logfire import LogfireSpan

from app.audit.domain.event import Stage
from app.logfire.failure_attrs import annotate_span_failure

# 低 cardinality 固定の span_name。stage / op / 識別子は attribute へ分離する。
_SPAN_NAME = "pipeline_stage"


class PipelineStageSpan:
    """非 AI worker 工程の記録口。result 語彙を持たず失敗分類の複写だけを担う。"""

    def __init__(self, span: LogfireSpan) -> None:
        self._span = span
        self._failure_set = False

    def record_failure(self, exc: Exception) -> None:
        """失敗分類属性を一度だけ焼く (no-override)。元の業務例外を最優先で残す。"""
        if self._failure_set:
            return
        annotate_span_failure(self._span, exc)
        self._failure_set = True


@contextmanager
def pipeline_stage_span(
    stage: Stage,
    *,
    op: str,
    source_id: int | None = None,
    article_id: int | None = None,
) -> Iterator[PipelineStageSpan]:
    """非 AI worker 工程を span で囲む。stage を ``Stage`` enum 値で強制注入する。

    op は task の操作名 (低 cardinality)。source_id / article_id は持つ工程だけ載せ、
    持たない run 単位工程 (briefing / trend_discovery / backfill) は省く。貫通例外は
    backstop が ``record_failure`` で分類複写し、握り潰す失敗は呼び出し側が明示する。
    """
    attrs: dict[str, object] = {"stage": stage.value, "op": op}
    if source_id is not None:
        attrs["source_id"] = source_id
    if article_id is not None:
        attrs["article_id"] = article_id
    with logfire.span(_SPAN_NAME, **attrs) as span:
        recorder = PipelineStageSpan(span)
        try:
            yield recorder
        except Exception as exc:
            recorder.record_failure(exc)
            raise
