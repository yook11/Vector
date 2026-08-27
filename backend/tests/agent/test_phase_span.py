"""``agent_phase()`` ヘルパーのspan生成契約と終了分類4値のunit test。

正本仕様: ``specs/agent-phase-span-vocabulary-slice.md`` の Invariants
「spanの生成は``agent_phase()``に集約する」「終了分類はヘルパーが持ち、
``agent_provider_call``と同型にする」と Test contract「span の生成」
「終了分類」。工程spanの終了分類の正本テストであり、各工程での``phase``値と
span新設は各サービスのtracing testが持つ。
"""

from __future__ import annotations

import asyncio

import pytest
from logfire.testing import CaptureLogfire
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.semconv.attributes.error_attributes import ERROR_TYPE
from opentelemetry.trace import StatusCode

from app.agent.contract import AnswerGenerationStopped
from app.agent.phase_span import agent_phase
from app.agent.runtime.contract import AgentResponseDefect, AgentResponseInvalidError
from app.analysis.ai_provider_errors import AIProviderNetworkError
from tests.logfire._span_helpers import (
    domain_attr_keys,
    exception_event,
    one_span_named,
    spans_named,
)

_PHASE_SPAN_NAME = "agent_phase"


def _one_raw_phase_span(capfire: CaptureLogfire) -> ReadableSpan:
    """OTel生spanから``agent_phase``のpending span重複を除いた1件を返す。

    ``exported_spans_as_dict()``はstatusを保持しないため、status検証だけは
    生spanを使う (``test_planner_tracing.py`` と同じ手法)。
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


# --- span の生成 -------------------------------------------------------


def test_span_name_is_agent_phase(capfire: CaptureLogfire) -> None:
    with agent_phase(phase="planning", agent_name="question_planner"):
        pass

    one_span_named(capfire, _PHASE_SPAN_NAME)


def test_agent_name_attribute_set_to_given_value(capfire: CaptureLogfire) -> None:
    with agent_phase(phase="planning", agent_name="question_planner"):
        pass

    span = one_span_named(capfire, _PHASE_SPAN_NAME)
    assert span["attributes"]["agent_name"] == "question_planner"


def test_agent_name_key_absent_when_omitted(capfire: CaptureLogfire) -> None:
    """内部検索のようにAgentでない機構が実行するspanは``agent_name``を持たない。"""
    with agent_phase(phase="evidence_collection", task_index=0):
        pass

    span = one_span_named(capfire, _PHASE_SPAN_NAME)
    assert "agent_name" not in domain_attr_keys(span["attributes"])


def test_task_index_attribute_set_to_given_value(capfire: CaptureLogfire) -> None:
    with agent_phase(
        phase="evidence_collection",
        agent_name="external_query_generator",
        task_index=2,
    ):
        pass

    span = one_span_named(capfire, _PHASE_SPAN_NAME)
    assert span["attributes"]["task_index"] == 2


def test_task_index_key_absent_when_omitted(capfire: CaptureLogfire) -> None:
    with agent_phase(phase="planning", agent_name="question_planner"):
        pass

    span = one_span_named(capfire, _PHASE_SPAN_NAME)
    assert "task_index" not in domain_attr_keys(span["attributes"])


def test_negative_task_index_raises_value_error_without_creating_span(
    capfire: CaptureLogfire,
) -> None:
    with pytest.raises(ValueError):
        with agent_phase(
            phase="evidence_collection",
            agent_name="external_query_generator",
            task_index=-1,
        ):
            pass

    assert spans_named(capfire, _PHASE_SPAN_NAME) == []


# --- 終了分類 (4値) -----------------------------------------------------


def test_normal_exit_keeps_span_status_unset_without_error_type(
    capfire: CaptureLogfire,
) -> None:
    with agent_phase(phase="planning", agent_name="question_planner"):
        pass

    span = one_span_named(capfire, _PHASE_SPAN_NAME)
    raw = _one_raw_phase_span(capfire)
    assert raw.status.status_code is StatusCode.UNSET
    assert ERROR_TYPE not in span["attributes"]
    assert exception_event(span) is None


def test_stopped_exception_keeps_span_unset_and_reraises_same_instance(
    capfire: CaptureLogfire,
) -> None:
    stopped = AnswerGenerationStopped()

    with pytest.raises(AnswerGenerationStopped) as raised:
        with agent_phase(phase="answering", agent_name="evidence_answer"):
            raise stopped

    assert raised.value is stopped
    span = one_span_named(capfire, _PHASE_SPAN_NAME)
    raw = _one_raw_phase_span(capfire)
    assert raw.status.status_code is StatusCode.UNSET
    assert exception_event(span) is None
    assert ERROR_TYPE not in span["attributes"]


def test_cancellation_is_unclassified_and_passes_through(
    capfire: CaptureLogfire,
) -> None:
    """cancelは工程が扱える終了ではないため、未分類として貫通させる。"""
    cancelled = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError) as raised:
        with agent_phase(phase="answering", agent_name="evidence_answer"):
            raise cancelled

    assert raised.value is cancelled
    span = one_span_named(capfire, _PHASE_SPAN_NAME)
    raw = _one_raw_phase_span(capfire)
    assert raw.status.status_code is StatusCode.ERROR
    assert exception_event(span) is not None
    assert ERROR_TYPE not in span["attributes"]


def test_ai_provider_error_marks_span_error_with_error_type_and_reraises_same_instance(
    capfire: CaptureLogfire,
) -> None:
    error = AIProviderNetworkError()

    with pytest.raises(AIProviderNetworkError) as raised:
        with agent_phase(
            phase="evidence_collection", agent_name="external_query_generator"
        ):
            raise error

    assert raised.value is error
    span = one_span_named(capfire, _PHASE_SPAN_NAME)
    raw = _one_raw_phase_span(capfire)
    assert raw.status.status_code is StatusCode.ERROR
    assert raw.status.description in (None, "")
    assert exception_event(span) is None
    assert span["attributes"][ERROR_TYPE] == error.CODE


def test_agent_response_invalid_error_marks_span_error_with_defect_error_type(
    capfire: CaptureLogfire,
) -> None:
    error = AgentResponseInvalidError(AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH)

    with pytest.raises(AgentResponseInvalidError) as raised:
        with agent_phase(phase="planning", agent_name="question_planner"):
            raise error

    assert raised.value is error
    span = one_span_named(capfire, _PHASE_SPAN_NAME)
    raw = _one_raw_phase_span(capfire)
    assert raw.status.status_code is StatusCode.ERROR
    assert raw.status.description in (None, "")
    assert exception_event(span) is None
    assert span["attributes"][ERROR_TYPE] == error.defect.value


def test_unclassified_exception_passes_through_to_logfire_auto_record(
    capfire: CaptureLogfire,
) -> None:
    """未分類例外はヘルパーが触らず、logfireの自動例外記録がERRORとeventを付ける。"""
    error = RuntimeError("boom")

    with pytest.raises(RuntimeError) as raised:
        with agent_phase(phase="planning", agent_name="question_planner"):
            raise error

    assert raised.value is error
    span = one_span_named(capfire, _PHASE_SPAN_NAME)
    raw = _one_raw_phase_span(capfire)
    assert raw.status.status_code is StatusCode.ERROR
    assert ERROR_TYPE not in span["attributes"]
    assert exception_event(span) is not None
