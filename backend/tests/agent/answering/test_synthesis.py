"""Answer synthesis service tests."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
from logfire.testing import CaptureLogfire

from app.agent.answering.audit import (
    AnswerSynthesisAttemptFailureEvent,
    AnswerSynthesisDefectEvent,
    AnswerSynthesisFinalEvent,
    AnswerSynthesisOutcomeCode,
    RequestRetryDisposition,
)
from app.agent.answering.evidence import AnswerEvidenceItem
from app.agent.answering.synthesis import (
    AnswerDraft,
    AnswerSynthesisService,
    RawAnswerDraft,
)
from app.agent.contract import ExternalUrlSource
from app.analysis.ai_provider_errors import AIProviderNetworkError
from tests.logfire._metric_helpers import collected_metrics, sum_counter_for_result

_SYNTHESIS_OUTCOME_METRIC = "vector.agent.answer_synthesis.outcome"


def _as_of() -> datetime:
    return datetime(2026, 7, 7, 9, 0, tzinfo=UTC)


def _evidence(ref: str = "1") -> AnswerEvidenceItem:
    return AnswerEvidenceItem(
        source=ExternalUrlSource(
            source_ref=ref,
            url=f"https://example.com/source-{ref}",
            title=f"source {ref}",
            snippet=f"claim {ref}",
        ),
        text=f"claim {ref}\nsnippet {ref}",
    )


def _raw(
    *,
    sufficiency: str = "answered",
    answer: object = "根拠から確認できます。",
    cited_refs: list[object] | None = None,
    missing_aspects: list[object] | None = None,
) -> RawAnswerDraft:
    return RawAnswerDraft(
        sufficiency=sufficiency,
        answer=answer,
        cited_refs=["1"] if cited_refs is None else cited_refs,
        missing_aspects=[] if missing_aspects is None else missing_aspects,
    )


class FakeGenerator:
    model_name = "fake-answer-model"
    prompt_version = "fake0001"

    def __init__(self, outcomes: Sequence[RawAnswerDraft | Exception]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    async def generate(
        self,
        *,
        question: str,
        evidence: list[AnswerEvidenceItem],
        as_of: datetime,
        target_time_window: str | None,
        previous_error: str | None = None,
    ) -> RawAnswerDraft:
        self.calls.append(
            {
                "question": question,
                "evidence": evidence,
                "as_of": as_of,
                "target_time_window": target_time_window,
                "previous_error": previous_error,
            }
        )
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeAnswerSynthesisAuditRecorder:
    def __init__(self) -> None:
        self.attempt_failures: list[AnswerSynthesisAttemptFailureEvent] = []
        self.defect_events: list[AnswerSynthesisDefectEvent] = []
        self.final_events: list[AnswerSynthesisFinalEvent] = []

    async def record_attempt_failure(
        self,
        event: AnswerSynthesisAttemptFailureEvent,
    ) -> None:
        self.attempt_failures.append(event)

    async def record_defect(self, event: AnswerSynthesisDefectEvent) -> None:
        self.defect_events.append(event)

    async def record_final_event(self, event: AnswerSynthesisFinalEvent) -> None:
        self.final_events.append(event)


class RaisingAnswerSynthesisAuditRecorder:
    async def record_attempt_failure(
        self,
        event: AnswerSynthesisAttemptFailureEvent,
    ) -> None:
        raise RuntimeError("audit recorder down")

    async def record_defect(self, event: AnswerSynthesisDefectEvent) -> None:
        raise RuntimeError("audit recorder down")

    async def record_final_event(self, event: AnswerSynthesisFinalEvent) -> None:
        raise RuntimeError("audit recorder down")


async def _synthesize(
    generator: FakeGenerator,
    *,
    recorder: FakeAnswerSynthesisAuditRecorder | None = None,
    evidence: list[AnswerEvidenceItem] | None = None,
) -> AnswerDraft:
    return await AnswerSynthesisService(
        generator=generator,
        audit_recorder=recorder,
    ).synthesize(
        question="NVIDIA の直近発表は投資判断に重要？",
        evidence=[_evidence()] if evidence is None else evidence,
        as_of=_as_of(),
        target_time_window="今日",
    )


def _metric_attributes(
    metrics: list[dict[str, Any]],
    metric_name: str,
) -> list[dict[str, Any]]:
    metric = next((item for item in metrics if item["name"] == metric_name), None)
    if metric is None:
        return []
    return [
        data_point.get("attributes", {}) for data_point in metric["data"]["data_points"]
    ]


@pytest.mark.asyncio
async def test_valid_raw_draft_passes_through_unchanged() -> None:
    generator = FakeGenerator([_raw(cited_refs=["1"])])
    recorder = FakeAnswerSynthesisAuditRecorder()

    draft = await _synthesize(generator, recorder=recorder)

    assert draft == AnswerDraft(
        sufficiency="answered",
        answer="根拠から確認できます。",
        cited_refs=["1"],
    )
    assert generator.calls[0]["previous_error"] is None
    assert recorder.attempt_failures == []
    assert recorder.defect_events == []
    assert len(recorder.final_events) == 1
    final = recorder.final_events[0]
    assert final.outcome_code is AnswerSynthesisOutcomeCode.SYNTHESIZED
    assert final.status == "answered"
    assert final.retry_used is False
    assert final.fallback_used is False
    assert final.defect_count == 0
    assert final.ai_model == "fake-answer-model"
    assert final.prompt_version == "fake0001"


@pytest.mark.asyncio
async def test_completes_insufficient_missing_aspects_and_records_defect() -> None:
    generator = FakeGenerator(
        [
            _raw(
                sufficiency="insufficient",
                answer="根拠の範囲では断定できません。",
                cited_refs=["1"],
                missing_aspects=[],
            )
        ]
    )
    recorder = FakeAnswerSynthesisAuditRecorder()

    draft = await _synthesize(generator, recorder=recorder)

    assert draft.sufficiency == "insufficient"
    assert draft.answer == "根拠の範囲では断定できません。"
    assert draft.cited_refs == ["1"]
    assert draft.missing_aspects
    assert len(generator.calls) == 1
    assert len(recorder.defect_events) == 1
    assert recorder.final_events[0].defect_count == 1


@pytest.mark.asyncio
async def test_removes_blank_and_duplicate_refs_and_missing_aspects() -> None:
    generator = FakeGenerator(
        [
            _raw(
                sufficiency="insufficient",
                answer="一部だけ確認できます。",
                cited_refs=["1", "", "1", "  ", "1"],
                missing_aspects=["", "会社側の一次情報", "会社側の一次情報", "\n"],
            )
        ]
    )
    recorder = FakeAnswerSynthesisAuditRecorder()

    draft = await _synthesize(generator, recorder=recorder)

    assert draft.cited_refs == ["1"]
    assert draft.missing_aspects == ["会社側の一次情報"]
    assert recorder.defect_events
    assert recorder.final_events[0].defect_count == len(recorder.defect_events)


@pytest.mark.asyncio
async def test_noncompletable_defect_retries_once_with_previous_error() -> None:
    repaired = _raw(
        sufficiency="answered",
        answer="修正後は根拠を引用しています。",
        cited_refs=["1"],
    )
    generator = FakeGenerator(
        [
            _raw(
                sufficiency="answered",
                answer="引用がありません。",
                cited_refs=[],
            ),
            repaired,
        ]
    )
    recorder = FakeAnswerSynthesisAuditRecorder()

    draft = await _synthesize(generator, recorder=recorder)

    assert draft.answer == "修正後は根拠を引用しています。"
    assert [call["previous_error"] for call in generator.calls][0] is None
    assert generator.calls[1]["previous_error"]
    assert [event.attempt_number for event in recorder.attempt_failures] == [1]
    assert recorder.final_events[0].attempt_count == 2
    assert recorder.final_events[0].retry_used is True


@pytest.mark.asyncio
async def test_persistent_noncompletable_defect_falls_back_to_valid_insufficient(
    capfire: CaptureLogfire,
) -> None:
    generator = FakeGenerator(
        [
            _raw(sufficiency="answered", answer="引用がありません。", cited_refs=[]),
            _raw(
                sufficiency="answered",
                answer="まだ引用がありません。",
                cited_refs=[],
            ),
        ]
    )
    recorder = FakeAnswerSynthesisAuditRecorder()

    draft = await _synthesize(generator, recorder=recorder)

    assert draft.sufficiency == "insufficient"
    assert draft.answer
    assert draft.cited_refs == []
    assert draft.missing_aspects
    assert [call["previous_error"] for call in generator.calls][0] is None
    assert generator.calls[1]["previous_error"]
    assert [event.attempt_number for event in recorder.attempt_failures] == [1, 2]
    final = recorder.final_events[0]
    assert final.outcome_code is AnswerSynthesisOutcomeCode.FALLBACK_USED
    assert final.status == "insufficient"
    assert final.fallback_used is True
    assert final.retry_used is True

    metrics = collected_metrics(capfire)
    assert sum_counter_for_result(metrics, _SYNTHESIS_OUTCOME_METRIC, "fallback") == 1


@pytest.mark.asyncio
async def test_unknown_citation_ref_is_detected_inside_synthesis_and_retried() -> None:
    generator = FakeGenerator(
        [
            _raw(
                sufficiency="answered",
                answer="存在しない根拠を引用しています。",
                cited_refs=["2"],
            ),
            _raw(
                sufficiency="answered",
                answer="実在する根拠を引用しています。",
                cited_refs=["1"],
            ),
        ]
    )
    recorder = FakeAnswerSynthesisAuditRecorder()

    draft = await _synthesize(generator, recorder=recorder)

    assert draft.cited_refs == ["1"]
    assert generator.calls[1]["previous_error"]
    assert recorder.attempt_failures[0].failure_kind == "ai_response_invalid"


@pytest.mark.asyncio
async def test_empty_evidence_answered_citation_falls_back_insufficient() -> None:
    generator = FakeGenerator(
        [
            _raw(
                sufficiency="answered",
                answer="根拠がないのに引用しています。",
                cited_refs=["1"],
            ),
            _raw(
                sufficiency="answered",
                answer="まだ根拠がないのに引用しています。",
                cited_refs=["1"],
            ),
        ]
    )

    draft = await _synthesize(generator, evidence=[])

    assert draft.sufficiency == "insufficient"
    assert draft.cited_refs == []
    assert draft.missing_aspects
    assert len(generator.calls) == 2


@pytest.mark.asyncio
async def test_empty_evidence_valid_insufficient_is_adopted_without_retry() -> None:
    generator = FakeGenerator(
        [
            _raw(
                sufficiency="insufficient",
                answer="検索で引用できる根拠は見つかりませんでした。一般論では参考程度に考えてください。",
                cited_refs=[],
                missing_aspects=["引用できる検索根拠"],
            )
        ]
    )

    draft = await _synthesize(generator, evidence=[])

    assert draft.sufficiency == "insufficient"
    assert draft.cited_refs == []
    assert draft.missing_aspects == ["引用できる検索根拠"]
    assert len(generator.calls) == 1


@pytest.mark.asyncio
async def test_provider_error_falls_back_without_retry() -> None:
    generator = FakeGenerator([AIProviderNetworkError()])
    recorder = FakeAnswerSynthesisAuditRecorder()

    draft = await _synthesize(generator, recorder=recorder)

    assert draft.sufficiency == "insufficient"
    assert len(generator.calls) == 1
    assert recorder.attempt_failures[0].request_retry_disposition is (
        RequestRetryDisposition.DO_NOT_RETRY_IN_REQUEST
    )
    assert recorder.final_events[0].retry_used is False


@pytest.mark.asyncio
async def test_unexpected_exception_propagates_without_fallback(
    capfire: CaptureLogfire,
) -> None:
    generator = FakeGenerator([RuntimeError("bug in generator")])
    recorder = FakeAnswerSynthesisAuditRecorder()

    with pytest.raises(RuntimeError, match="bug in generator"):
        await _synthesize(generator, recorder=recorder)

    assert len(generator.calls) == 1
    assert recorder.final_events == []
    metrics = collected_metrics(capfire)
    assert _metric_attributes(metrics, _SYNTHESIS_OUTCOME_METRIC) == []


@pytest.mark.asyncio
async def test_recorder_errors_do_not_stop_synthesis() -> None:
    generator = FakeGenerator(
        [
            _raw(
                sufficiency="insufficient",
                answer="一部だけ確認できます。",
                cited_refs=["1"],
                missing_aspects=[],
            )
        ]
    )

    draft = await AnswerSynthesisService(
        generator=generator,
        audit_recorder=RaisingAnswerSynthesisAuditRecorder(),
    ).synthesize(
        question="NVIDIA の直近発表は投資判断に重要？",
        evidence=[_evidence()],
        as_of=_as_of(),
        target_time_window="今日",
    )

    assert draft.sufficiency == "insufficient"
    assert draft.missing_aspects


@pytest.mark.asyncio
async def test_outcome_metric_records_synthesized_once(
    capfire: CaptureLogfire,
) -> None:
    generator = FakeGenerator([_raw(cited_refs=["1"])])

    await _synthesize(generator)

    metrics = collected_metrics(capfire)
    assert (
        sum_counter_for_result(metrics, _SYNTHESIS_OUTCOME_METRIC, "synthesized") == 1
    )
    attrs = _metric_attributes(metrics, _SYNTHESIS_OUTCOME_METRIC)
    dumped = json.dumps(metrics, ensure_ascii=False, default=str)
    assert "NVIDIA の直近発表" not in dumped
    assert attrs == [
        {
            "result": "synthesized",
            "retry_used": False,
            "status": "answered",
            "fallback_used": False,
        }
    ]
