"""例外の自由文を Logfire export 前に落とす span redactor。

例外が ``logfire.span`` を貫通すると OTel が span に ``exception`` event を記録し、
``exception.message`` / ``exception.stacktrace`` に ``str(exc)`` + traceback を残す。
さらに logfire の ``set_exception_status`` が ``span.status.description`` に
``"{ExcClass}: {exc}"`` を、``ValidationError`` 時は ``record_exception`` が失敗入力を
``exception.logfire.data`` キーで exception event 属性と span 属性の両方に焼く。
logfire scrubber は ``exception.message/stacktrace/type`` を ``SAFE_KEYS`` で素通しし、
``exception.logfire.data`` も値の機微キーワード一致でしか落とさないため、任意 PII を含む
生例外文字列/入力がそのまま Logfire backend へ送られる。これを export 境界で
``[redacted]`` に置換し、``exception.type`` / ``status_code`` (種別・状態) は残す。

対象は span の自動例外面のみ: exception event 属性・status.description・span 属性の
``exception.*`` 自由文キー。手書きで ``str(exc)`` をメッセージ/属性値に補間した場合や
OTel LogRecord 経路は射程外で、logfire scrubber と raise 側の規律に委ねる。

公式フックは (A) instrument_fastapi の生 OTel span と (B) ``logfire.span`` の
両方を 1 機構で塞げない (scrub callback は SAFE_KEYS、exception_callback は (A) で
未発火)。よって configure 後に ``MainSpanProcessorWrapper`` 下流 (export 直前) を
wrapper で包む。内部 API 依存のため構造が想定外なら fail-fast し、回帰テストで
version 上げを検知する。
"""

from __future__ import annotations

from logfire._internal.constants import ATTRIBUTES_VALIDATION_ERROR_KEY
from logfire._internal.exporters.processor_wrapper import MainSpanProcessorWrapper
from logfire._internal.exporters.wrapper import WrapperSpanProcessor
from logfire._internal.utils import span_to_dict
from opentelemetry.sdk.trace import Event, ReadableSpan, SpanProcessor
from opentelemetry.trace import Status

_REDACTED = "[redacted]"

# 例外貫通で生 str(exc)/入力を載せる自由文 attribute キー。SAFE_KEYS 等で scrub
# されないため、event 属性・span 属性のどこに現れても落とす。type/escaped/fingerprint
# (PII フリーの分類・相関値) は残す。
_FREETEXT_KEYS = frozenset(
    {"exception.message", "exception.stacktrace", ATTRIBUTES_VALIDATION_ERROR_KEY}
)


class ExceptionRedactingProcessor(WrapperSpanProcessor):
    """export 直前に exception の自由文を落とす。type / status_code は残す。"""

    def on_end(self, span: ReadableSpan) -> None:
        super().on_end(_redact_span(span))


def _redact_mapping(attrs: object) -> dict:
    """自由文キーを ``[redacted]`` に置換した新 dict を返す。"""
    return {k: (_REDACTED if k in _FREETEXT_KEYS else v) for k, v in attrs.items()}  # type: ignore[union-attr]


def _redact_event(ev: Event) -> Event:
    """exception event の自由文属性を redact。非 exception event はそのまま返す。"""
    attrs = ev.attributes or {}
    if ev.name != "exception" or _FREETEXT_KEYS.isdisjoint(attrs):
        return ev
    return Event(
        name=ev.name, attributes=_redact_mapping(attrs), timestamp=ev.timestamp
    )


def _redact_span(span: ReadableSpan) -> ReadableSpan:
    """exception 由来の自由文 (event 属性 / span 属性 / status) を redact。"""
    attrs = span.attributes or {}
    has_exc = any(e.name == "exception" for e in span.events)
    has_attr = not _FREETEXT_KEYS.isdisjoint(attrs)
    has_desc = bool(span.status.description)
    if not (has_exc or has_attr or has_desc):
        return span
    d = span_to_dict(span)
    if has_exc:
        d["events"] = [_redact_event(e) for e in d["events"]]
    if has_attr:
        d["attributes"] = _redact_mapping(d["attributes"])
    if has_desc:
        # status.description は logfire の set_exception_status が str(exc) からのみ書き
        # OTel は ERROR 状態でしか保持しない。任意 PII を脅威とするため良性か判別せず、
        # 非空 description は一律 unsafe として落とす (fail-safe over-redaction)。
        d["status"] = Status(status_code=d["status"].status_code, description=_REDACTED)
    return ReadableSpan(**d)


def _find_main_wrapper(
    procs: tuple[SpanProcessor, ...],
) -> MainSpanProcessorWrapper | None:
    """processor チェーンを辿り logfire の ``MainSpanProcessorWrapper`` を返す。"""
    stack = list(procs)
    while stack:
        p = stack.pop()
        if isinstance(p, MainSpanProcessorWrapper):
            return p
        inner = getattr(p, "processor", None)
        if inner is not None:
            stack.append(inner)
    return None


def install_exception_redaction() -> None:
    """``logfire.configure`` 後に Main 下流 (root_processor) を redactor で包む。

    Main の dedupe/scrub/level 設定を先に走らせ、redactor は export 直前に動く。
    想定構造が無い/空なら fail-fast (沈黙 no-op で漏洩を続けない)。冪等。
    """
    from opentelemetry import trace

    provider = trace.get_tracer_provider()
    sdk = getattr(provider, "provider", provider)
    smp = getattr(sdk, "_active_span_processor", None)
    procs = getattr(smp, "_span_processors", None)
    if not procs:
        raise RuntimeError(
            "logfire span processor chain empty/absent; redaction not installed"
        )
    main = _find_main_wrapper(procs)
    if main is None:
        raise RuntimeError(
            "MainSpanProcessorWrapper not found; exception redaction not installed"
        )
    if isinstance(main.processor, ExceptionRedactingProcessor):
        return
    # setup_logfire はプロセス起動時に単一スレッドで 1 度だけ呼ぶため lock は不要。
    main.processor = ExceptionRedactingProcessor(main.processor)
