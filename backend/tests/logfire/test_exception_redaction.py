"""``install_exception_redaction`` の export 境界 redaction を capfire で検証する。

例外が span を貫通すると OTel は ``exception`` event (message/stacktrace) を、
logfire は ``status.description`` を生 ``str(exc)`` で残し、scrubber も素通しする。
本テストは redactor が export span (raw ``capfire.exporter.exported_spans``) で
これらを ``[redacted]`` に落とし ``exception.type`` / ``status_code`` を残すことを、
(B) worker span と (A) fastapi server span の両方で確認する。負のコントロールで
「redactor が無ければ漏れる」teeth を担保する。

capfire は関数スコープで毎回 ``logfire.configure`` し provider を作り直すため、
install を呼ぶテストと呼ばないテストの間で redactor は持ち越されない。
"""

from __future__ import annotations

import logfire
import pytest
from fastapi import FastAPI
from logfire.testing import CaptureLogfire
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import StatusCode
from pydantic import BaseModel, ValidationError
from starlette.testclient import TestClient

from app.audit.domain.event import Stage
from app.logfire.redaction import (
    ExceptionRedactingProcessor,
    _find_main_wrapper,
    install_exception_redaction,
)
from app.logfire.stage_span import pipeline_stage_span

_PII = "PIIMARKER_x@example.com"
_SECRET = "password=hunter2"
_MESSAGE = f"boom {_PII} {_SECRET}"
_DATA_KEY = "exception.logfire.data"


class _IntModel(BaseModel):
    value: int


def _span_text(span: ReadableSpan) -> str:
    """span に乗る文字列値 (status.description / attribute / event attr) を連結する。"""
    parts = [span.status.description or ""]
    parts += [str(v) for v in (span.attributes or {}).values()]
    for ev in span.events:
        parts += [str(v) for v in (ev.attributes or {}).values()]
    return "\n".join(parts)


def _exception_event(span: ReadableSpan):  # noqa: ANN202
    return next((e for e in span.events if e.name == "exception"), None)


def _final_pipeline_span(capfire: CaptureLogfire) -> ReadableSpan:
    """pipeline_stage の最終 span を返す (開始時の pending_span は除外)。"""
    return next(
        s
        for s in capfire.exporter.exported_spans
        if s.name == "pipeline_stage"
        and (s.attributes or {}).get("logfire.span_type") == "span"
    )


def _emit_worker_exception(capfire: CaptureLogfire, *, install: bool) -> ReadableSpan:
    """worker span 内で例外を貫通させ、export された pipeline_stage span を返す。"""
    if install:
        install_exception_redaction()
    with pytest.raises(RuntimeError):  # noqa: PT012
        with pipeline_stage_span(Stage.CURATION, op="t"):
            raise RuntimeError(_MESSAGE)
    return _final_pipeline_span(capfire)


# (B) worker span — message / stacktrace / status.description / type / status_code


def test_worker_exception_message_redacted(capfire: CaptureLogfire) -> None:
    span = _emit_worker_exception(capfire, install=True)
    event = _exception_event(span)
    assert event is not None
    assert event.attributes["exception.message"] == "[redacted]"


def test_worker_exception_stacktrace_redacted(capfire: CaptureLogfire) -> None:
    span = _emit_worker_exception(capfire, install=True)
    event = _exception_event(span)
    assert event is not None
    assert event.attributes["exception.stacktrace"] == "[redacted]"


def test_worker_exception_type_preserved(capfire: CaptureLogfire) -> None:
    span = _emit_worker_exception(capfire, install=True)
    event = _exception_event(span)
    assert event is not None
    assert str(event.attributes["exception.type"]).endswith("RuntimeError")


def test_worker_status_description_redacted(capfire: CaptureLogfire) -> None:
    """status.description の生 str(exc) を落とすが ERROR 状態は残す ([P0])。"""
    span = _emit_worker_exception(capfire, install=True)
    assert span.status.status_code == StatusCode.ERROR
    assert span.status.description == "[redacted]"


def test_worker_no_pii_anywhere(capfire: CaptureLogfire) -> None:
    span = _emit_worker_exception(capfire, install=True)
    text = _span_text(span)
    assert _PII not in text
    assert _SECRET not in text


def test_worker_without_redaction_leaks_pii(capfire: CaptureLogfire) -> None:
    """redactor を入れなければ PII が export span に残る (teeth)。"""
    span = _emit_worker_exception(capfire, install=False)
    assert _PII in _span_text(span)


# ValidationError 経路 — exception.logfire.data (失敗入力) も落とす


def test_worker_validation_error_input_redacted(capfire: CaptureLogfire) -> None:
    """pydantic ValidationError の失敗入力 (exception.logfire.data) を redact する。

    logfire の record_exception は ValidationError 時に失敗入力 JSON を
    exception.logfire.data キーで span 属性と exception event 属性の両方に焼く。
    SAFE_KEYS 外だが scrubber は機微キーワードしか落とさないため任意 PII が残る。
    """
    install_exception_redaction()
    with pytest.raises(ValidationError):  # noqa: PT012
        with pipeline_stage_span(Stage.CURATION, op="t"):
            _IntModel(value=_PII)
    span = _final_pipeline_span(capfire)

    # 失敗入力 (_PII) が span のどこにも残らない。
    assert _PII not in _span_text(span)
    # teeth: exception.logfire.data が実際に発火しており [redacted] 化されている
    # (RuntimeError では生まれないキー。空虚な緑通過を防ぐ)。
    event = _exception_event(span)
    emitted = [
        v
        for v in (
            (span.attributes or {}).get(_DATA_KEY),
            (event.attributes.get(_DATA_KEY) if event is not None else None),
        )
        if v is not None
    ]
    assert emitted, "exception.logfire.data not emitted; test would be vacuous"
    assert all(v == "[redacted]" for v in emitted)


# (A) fastapi server span — instrument_fastapi の生 OTel span も redact される


def test_fastapi_server_span_exception_redacted(capfire: CaptureLogfire) -> None:
    """未処理例外を持つ全 span の exception event が redact され PII が残らない。"""
    install_exception_redaction()
    app = FastAPI()
    logfire.instrument_fastapi(app, capture_headers=False)

    @app.get("/boom")
    def boom() -> None:  # noqa: ANN202
        raise ValueError(_MESSAGE)

    with TestClient(app, raise_server_exceptions=False) as test_client:
        response = test_client.get("/boom")
    assert response.status_code == 500

    exc_spans = [s for s in capfire.exporter.exported_spans if _exception_event(s)]
    assert exc_spans, "no span carried an exception event"
    for span in exc_spans:
        event = _exception_event(span)
        assert event.attributes["exception.message"] == "[redacted]"
        assert _PII not in _span_text(span)


# 非破壊 — 例外を持たない通常 span は素通し


def test_normal_span_not_mangled(capfire: CaptureLogfire) -> None:
    install_exception_redaction()
    with pipeline_stage_span(Stage.CURATION, op="t", source_id=7):
        pass
    span = _final_pipeline_span(capfire)
    assert _exception_event(span) is None
    assert span.attributes["source_id"] == 7
    assert span.status.description in (None, "")


def test_existing_attribute_scrubbing_preserved(capfire: CaptureLogfire) -> None:
    """redactor 設置後も logfire の通常 scrubbing (secret キー値) は効く (I-R2)。

    redactor は exception 由来キーしか触らないため、`password` 等の secret 属性は
    logfire scrubber が export 前に落とす経路がそのまま残ることを確認する。
    """
    install_exception_redaction()
    with logfire.span("scrub_probe", password="hunter2_secret"):
        pass
    span = next(
        s
        for s in capfire.exporter.exported_spans
        if s.name == "scrub_probe"
        and (s.attributes or {}).get("logfire.span_type") == "span"
    )
    assert "hunter2_secret" not in _span_text(span)
    assert "Scrubbed" in str(span.attributes["password"])


# install_exception_redaction の契約 — fail-fast / 冪等


def test_install_is_idempotent(capfire: CaptureLogfire) -> None:
    """二重 install しても redactor は 1 段しか積まれない (二重ラップしない)。"""
    install_exception_redaction()
    install_exception_redaction()
    provider = trace.get_tracer_provider()
    sdk = getattr(provider, "provider", provider)
    main = _find_main_wrapper(sdk._active_span_processor._span_processors)
    assert isinstance(main.processor, ExceptionRedactingProcessor)
    assert not isinstance(main.processor.processor, ExceptionRedactingProcessor)


def test_install_fails_fast_when_chain_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """想定の processor チェーンが無ければ沈黙 no-op でなく RuntimeError で落ちる。"""
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: object())
    with pytest.raises(RuntimeError, match="redaction not installed"):
        install_exception_redaction()
