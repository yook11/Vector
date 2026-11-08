"""provider-attempt span 契約テストの観測 helper。"""

from __future__ import annotations

from typing import Any

from logfire.testing import CaptureLogfire
from opentelemetry.sdk.trace import ReadableSpan

_PROVIDER_ATTEMPT_SPAN_NAME = "agent_provider_call"
_FRAMEWORK_PREFIXES = ("logfire.", "code.")
_STANDARD_ATTRIBUTE_KEYS = frozenset(
    {
        "error.type",
        "gen_ai.operation.name",
        "gen_ai.provider.name",
        "gen_ai.request.model",
        "gen_ai.response.model",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "gen_ai.usage.cache_read.input_tokens",
        "gen_ai.usage.reasoning.output_tokens",
    }
)


def provider_attempt_spans(capfire: CaptureLogfire) -> list[ReadableSpan]:
    return [
        span
        for span in capfire.exporter.exported_spans
        if span.name == _PROVIDER_ATTEMPT_SPAN_NAME
        and (span.attributes or {}).get("logfire.span_type") == "span"
    ]


def one_provider_attempt_span(capfire: CaptureLogfire) -> ReadableSpan:
    spans = provider_attempt_spans(capfire)
    assert len(spans) == 1, (
        f"expected one {_PROVIDER_ATTEMPT_SPAN_NAME} span, got {len(spans)}"
    )
    return spans[0]


def application_attribute_keys(span: ReadableSpan) -> set[str]:
    return {
        key
        for key in (span.attributes or {})
        if not key.startswith(_FRAMEWORK_PREFIXES)
        and key not in _STANDARD_ATTRIBUTE_KEYS
    }


def span_text(span: ReadableSpan) -> str:
    parts = [span.name, span.status.description or ""]
    for key, value in (span.attributes or {}).items():
        parts.extend((key, str(value)))
    for event in span.events:
        parts.append(event.name)
        for key, value in (event.attributes or {}).items():
            parts.extend((key, str(value)))
    return "\n".join(parts)


def exception_events(span: ReadableSpan) -> list[Any]:
    return [event for event in span.events if event.name == "exception"]
