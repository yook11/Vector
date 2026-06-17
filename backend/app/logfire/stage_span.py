"""非 AI worker 工程の Logfire span helper。

``article_stage`` (AI 分析 3 工程) が持つ result 語彙を非 AI 工程は持たないため、
``Stage`` を強制注入するだけの薄い context manager を 1 本だけ置く。span は
taskiq ``OpenTelemetryMiddleware`` の ``execute/<task_name>`` span の子として開き、
「どの工程で・どの場所で例外が起きたか」を Logfire の stage 軸で絞り込めるようにする。

span attribute には本文・URL・prompt・AI response・認証情報を載せない。低 cardinality
の語彙 (stage / op) と内部 DB ID (source_id / article_id) のみを載せる。span 内を
貫通した例外は ``logfire.span`` が OTel exception event として自動記録し level を error
に上げる。失敗種別 (failure_kind / code / error_class) の複写は後続 PR で足す。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import logfire
from logfire import LogfireSpan

from app.audit.domain.event import Stage

# 低 cardinality 固定の span_name。stage / op / 識別子は attribute へ分離する。
_SPAN_NAME = "pipeline_stage"


@contextmanager
def pipeline_stage_span(
    stage: Stage,
    *,
    op: str,
    source_id: int | None = None,
    article_id: int | None = None,
) -> Iterator[LogfireSpan]:
    """非 AI worker 工程を span で囲む。stage を ``Stage`` enum 値で強制注入する。

    op は task の操作名 (低 cardinality)。source_id / article_id は持つ工程だけ載せ、
    持たない run 単位工程 (briefing / trend_discovery / backfill) は省く。
    """
    attrs: dict[str, object] = {"stage": stage.value, "op": op}
    if source_id is not None:
        attrs["source_id"] = source_id
    if article_id is not None:
        attrs["article_id"] = article_id
    with logfire.span(_SPAN_NAME, **attrs) as span:
        yield span
