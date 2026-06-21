"""span に失敗分類属性を焼く共有 helper。

``pipeline_stage`` / ``article_stage`` の両 span helper が、貫通例外の backstop と
握り潰し失敗の明示記録の双方で同一語彙 (``pipeline_events`` と同じ failure_kind /
code / retryability) を span に複写するために使う。失敗分類は ``project_failure``
に一元化し、ここは span への複写だけを担う (分類ロジックは再実装しない)。
"""

from __future__ import annotations

from logfire import LogfireSpan

from app.audit.error_fields import exception_fqn
from app.audit.failure_projection import project_failure


def annotate_span_failure(span: LogfireSpan, exc: Exception) -> None:
    """例外を failure projection に投影し失敗分類属性を span に焼く。

    本文・URL・prompt は載せず ``project_failure`` / ``exception_fqn`` が返す低〜中
    cardinality の分類値のみ。``failure_action`` は値がある時だけ載せる
    (現状は drop_article のみ)。引数型を ``Exception`` に絞り協調キャンセル
    (BaseException 系) を対象外にする。
    """
    projection = project_failure(exc)
    span.set_attribute("failure_kind", projection.failure_kind)
    span.set_attribute("code", projection.code)
    span.set_attribute("retryability", projection.retryability.value)
    span.set_attribute("error_class", exception_fqn(exc))
    if projection.failure_action is not None:
        span.set_attribute("failure_action", projection.failure_action.value)
