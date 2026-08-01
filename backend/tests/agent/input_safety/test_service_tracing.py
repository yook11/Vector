"""InputSafetyService.check() のagent_phase span契約。

正本仕様: ``specs/agent-phase-span-vocabulary-slice.md`` の
「工程とspanの対応」#1 (safety_check)、Test contract「新設したspan」。
終了分類自体(未分類/停止/分類済み失敗の判定)の正本は``test_phase_span.py``で
あり、ここでは「safety_check工程のspanが実際にRun単位1回作られ、各経路で
その分類を受ける」ことだけを見る。
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

import pytest
from logfire.testing import CaptureLogfire
from opentelemetry.semconv.attributes.error_attributes import ERROR_TYPE
from opentelemetry.trace import StatusCode

from app.agent.input_safety.agent import INPUT_SAFETY_AGENT
from app.agent.input_safety.contract import (
    InputSafetyAgentOutput,
    InputSafetyBlockReason,
)
from app.agent.input_safety.service import InputSafetyService
from app.analysis.ai_provider_errors import (
    AIProviderContentRejectionKind,
    AIProviderInputRejectedError,
    AIProviderNetworkError,
)
from tests.agent.input_safety._helpers import RecordingRuntimeScopeFactory
from tests.agent.runtime._fakes import ScriptedAgentRuntime
from tests.logfire._span_helpers import domain_attr_keys, exception_event, spans_named

_PHASE_SPAN_NAME = "agent_phase"
_RUN_ID = UUID("00000000-0000-4000-a000-000000000099")


class _ProviderNeutralContentReason(StrEnum):
    """provider生reasonを模す、input safety自身のblock reason語彙とは別軸のtest用値。"""

    FILTERED = "filtered"


def _service(
    outcomes: list[object | BaseException],
) -> InputSafetyService:
    runtime = ScriptedAgentRuntime(outcomes)
    factory = RecordingRuntimeScopeFactory(runtime)
    return InputSafetyService(agent=INPUT_SAFETY_AGENT, runtime_scope_factory=factory)


def _one_raw_phase_span(capfire: CaptureLogfire):
    """OTel生spanからagent_phaseのpending span重複を除いた1件を返す。

    ``exported_spans_as_dict()``はstatusを保持しないため、status検証だけは
    生spanを使う(``test_phase_span.py``と同じ手法)。
    """
    raw_spans = [
        span
        for span in capfire.exporter.exported_spans
        if span.name == _PHASE_SPAN_NAME
        and (span.attributes or {}).get("logfire.span_type") == "span"
    ]
    assert len(raw_spans) == 1, (
        f"expected exactly 1 raw {_PHASE_SPAN_NAME} span, got {len(raw_spans)}"
    )
    return raw_spans[0]


async def test_allow_creates_one_run_scoped_span_without_task_index(
    capfire: CaptureLogfire,
) -> None:
    service = _service(
        [
            InputSafetyAgentOutput(
                input_safety_result="allow",
                block_reason=None,
            )
        ]
    )

    result = await service.check(
        question="question", previous_turn=None, run_id=_RUN_ID
    )

    assert result.is_blocked is False
    spans = spans_named(capfire, _PHASE_SPAN_NAME)
    assert len(spans) == 1
    span = spans[0]
    assert domain_attr_keys(span["attributes"]) == {"phase", "agent_name"}
    assert span["attributes"]["phase"] == "safety_check"
    assert span["attributes"]["agent_name"] == INPUT_SAFETY_AGENT.name
    raw = _one_raw_phase_span(capfire)
    assert raw.status.status_code is StatusCode.UNSET
    assert exception_event(span) is None


async def test_classifier_block_keeps_span_status_unset(
    capfire: CaptureLogfire,
) -> None:
    """ブロックは例外ではなく戻り値なので、spanはERRORにならない。"""
    service = _service(
        [
            InputSafetyAgentOutput(
                input_safety_result="block",
                block_reason=InputSafetyBlockReason.SELF_HARM_INSTRUCTIONS,
            )
        ]
    )

    result = await service.check(
        question="question", previous_turn=None, run_id=_RUN_ID
    )

    assert result.is_blocked is True
    raw = _one_raw_phase_span(capfire)
    assert raw.status.status_code is StatusCode.UNSET
    span = spans_named(capfire, _PHASE_SPAN_NAME)[0]
    assert exception_event(span) is None


async def test_provider_safety_block_keeps_span_status_unset(
    capfire: CaptureLogfire,
) -> None:
    """providerのsafety filterによるblockもcheck()内で握られ戻り値になるため、

    spanはERRORにならない。
    """
    provider_error = AIProviderInputRejectedError(
        reason=_ProviderNeutralContentReason.FILTERED,
        rejection_kind=AIProviderContentRejectionKind.SAFETY,
    )
    service = _service([provider_error])

    result = await service.check(
        question="question", previous_turn=None, run_id=_RUN_ID
    )

    assert result.block_reason == InputSafetyBlockReason.PROVIDER_SAFETY_FILTER
    raw = _one_raw_phase_span(capfire)
    assert raw.status.status_code is StatusCode.UNSET
    span = spans_named(capfire, _PHASE_SPAN_NAME)[0]
    assert exception_event(span) is None


async def test_classified_provider_error_marks_span_error_with_error_type(
    capfire: CaptureLogfire,
) -> None:
    """check()が再送出するAIProviderErrorは、agent_phaseの終了分類どおりspanが

    ERRORになりerror.typeが付く(agent_phase自体の分類網羅はtest_phase_span.py
    が正本、ここは「安全確認工程のspanが実際にその分類を受ける」ことだけを見る)。
    """
    error = AIProviderNetworkError()
    service = _service([error])

    with pytest.raises(AIProviderNetworkError) as raised:
        await service.check(question="question", previous_turn=None, run_id=_RUN_ID)

    assert raised.value is error
    raw = _one_raw_phase_span(capfire)
    assert raw.status.status_code is StatusCode.ERROR
    span = spans_named(capfire, _PHASE_SPAN_NAME)[0]
    assert exception_event(span) is None
    assert span["attributes"][ERROR_TYPE] == error.CODE
