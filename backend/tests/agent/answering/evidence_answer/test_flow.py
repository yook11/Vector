"""Evidence answer flow tests."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from importlib import import_module
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
from app.agent.answering.evidence_answer.contract import (
    EvidenceAnswerDraft,
    EvidenceAnswerDraftGenerationInvalidError,
    RawEvidenceAnswerDraft,
)
from app.agent.answering.evidence_answer.evidence import AnswerEvidenceItem
from app.agent.answering.evidence_answer.flow import EvidenceAnswerFlow
from app.agent.contract import ExternalUrlSource
from app.analysis.ai_provider_errors import AIProviderError, AIProviderNetworkError
from tests.logfire._metric_helpers import collected_metrics, sum_counter_for_result

_SYNTHESIS_OUTCOME_METRIC = "vector.agent.answer_synthesis.outcome"
_DEFECT_CITED_REFS_RECOMPUTED = "cited_refs_recomputed_from_markers"


def _as_of() -> datetime:
    return datetime(2026, 7, 7, 9, 0, tzinfo=UTC)


def _evidence(ref: str = "1") -> AnswerEvidenceItem:
    return AnswerEvidenceItem(
        source=ExternalUrlSource(
            source_ref=ref,
            url=f"https://example.com/source-{ref}",
            title=f"source {ref}",
            evidence_claim=f"claim {ref}",
        ),
        text=f"claim {ref}\nsnippet {ref}",
    )


def _raw(
    *,
    sufficiency: str = "answered",
    answer: object = "根拠から確認できます。[[1]]",
    cited_refs: list[object] | None = None,
    missing_aspects: list[object] | None = None,
) -> RawEvidenceAnswerDraft:
    return RawEvidenceAnswerDraft(
        sufficiency=sufficiency,
        answer=answer,
        cited_refs=["1"] if cited_refs is None else cited_refs,
        missing_aspects=[] if missing_aspects is None else missing_aspects,
    )


def _raw_json(raw: RawEvidenceAnswerDraft) -> str:
    return json.dumps(raw.model_dump(mode="json"), ensure_ascii=False)


def _operation_names(reporter: RecordingDeltaReporter) -> list[tuple[str, int]]:
    return [(name, generation) for name, generation, _ in reporter.operations]


StreamOutcome = RawEvidenceAnswerDraft | str | Sequence[str] | Exception


class FakeEvidenceAnswerStream:
    def __init__(self, outcome: StreamOutcome) -> None:
        if isinstance(outcome, Exception):
            self._items: list[str | Exception] = [outcome]
        elif isinstance(outcome, RawEvidenceAnswerDraft):
            self._items = [
                json.dumps(outcome.model_dump(mode="json"), ensure_ascii=False)
            ]
        elif isinstance(outcome, str):
            self._items = [outcome]
        else:
            self._items = list(outcome)
        self.closed = False

    def __aiter__(self) -> FakeEvidenceAnswerStream:
        return self

    async def __anext__(self) -> str:
        if not self._items:
            raise StopAsyncIteration
        item = self._items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def aclose(self) -> None:
        self.closed = True


class FakeGenerator:
    model_name = "fake-answer-model"
    prompt_version = "fake0001"

    def __init__(self, outcomes: Sequence[StreamOutcome]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []
        self.streams: list[FakeEvidenceAnswerStream] = []

    def stream(
        self,
        *,
        question: str,
        evidence: list[AnswerEvidenceItem],
        as_of: datetime,
        target_time_window: str | None,
        user_intent: str = "",
        prior_coverage: str = "",
        user_activity_context: str = "",
        previous_error: str | None = None,
    ) -> AsyncIterator[str]:
        self.calls.append(
            {
                "question": question,
                "evidence": evidence,
                "as_of": as_of,
                "target_time_window": target_time_window,
                "user_intent": user_intent,
                "prior_coverage": prior_coverage,
                "user_activity_context": user_activity_context,
                "previous_error": previous_error,
            }
        )
        outcome = self._outcomes.pop(0)
        stream = FakeEvidenceAnswerStream(outcome)
        self.streams.append(stream)
        return stream


class RecordingDeltaReporter:
    def __init__(self, *, fail_on: frozenset[str] = frozenset()) -> None:
        self.fail_on = fail_on
        self.appended: list[tuple[int, str]] = []
        self.finished: list[int] = []
        self.aborted: list[int] = []
        self.reset_generations: list[int] = []
        self.operations: list[tuple[str, int, str | None]] = []

    async def append(self, *, generation: int, text: str) -> None:
        self.appended.append((generation, text))
        self.operations.append(("append", generation, text))
        if "append" in self.fail_on:
            raise RuntimeError("REPORTER_APPEND_SECRET")

    async def finish(self, *, generation: int) -> None:
        self.finished.append(generation)
        self.operations.append(("finish", generation, None))
        if "finish" in self.fail_on:
            raise RuntimeError("REPORTER_FINISH_SECRET")

    async def abort(self, *, generation: int) -> None:
        self.aborted.append(generation)
        self.operations.append(("abort", generation, None))
        if "abort" in self.fail_on:
            raise RuntimeError("REPORTER_ABORT_SECRET")

    async def reset(self, *, generation: int) -> None:
        self.reset_generations.append(generation)
        self.operations.append(("reset", generation, None))
        if "reset" in self.fail_on:
            raise RuntimeError("REPORTER_RESET_SECRET")


class SequenceContinuation:
    def __init__(self, results: Sequence[bool]) -> None:
        self._results = list(results)
        self.calls = 0

    async def should_continue(self) -> bool:
        self.calls += 1
        if not self._results:
            return True
        return self._results.pop(0)


def _answer_generation_stopped_type() -> type[BaseException]:
    contract = import_module("app.agent.contract")
    stopped_type = getattr(contract, "AnswerGenerationStopped", None)
    assert stopped_type is not None, "shared AnswerGenerationStopped が未実装です"
    assert isinstance(stopped_type, type) and issubclass(stopped_type, BaseException)
    return stopped_type


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


async def _answer(
    generator: FakeGenerator,
    *,
    recorder: (
        FakeAnswerSynthesisAuditRecorder | RaisingAnswerSynthesisAuditRecorder | None
    ) = None,
    evidence: list[AnswerEvidenceItem] | None = None,
    delta_reporter: RecordingDeltaReporter | None = None,
    continuation: SequenceContinuation | None = None,
) -> EvidenceAnswerDraft:
    flow_kwargs: dict[str, Any] = {
        "generator": generator,
        "audit_recorder": recorder,
    }
    if delta_reporter is not None:
        flow_kwargs["delta_reporter"] = delta_reporter
    if continuation is not None:
        flow_kwargs["continuation"] = continuation
    return await EvidenceAnswerFlow(**flow_kwargs).answer(
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

    draft = await _answer(generator, recorder=recorder)

    assert draft == EvidenceAnswerDraft(
        sufficiency="answered",
        answer="根拠から確認できます。[[1]]",
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
async def test_derives_refs_from_markers_and_records_mismatch_defect() -> None:
    generator = FakeGenerator(
        [
            _raw(
                answer="根拠 1 から確認できます。[[1]]",
                cited_refs=[],
            )
        ]
    )
    recorder = FakeAnswerSynthesisAuditRecorder()

    draft = await _answer(generator, recorder=recorder)

    assert draft.cited_refs == ["1"]
    assert draft.answer == "根拠 1 から確認できます。[[1]]"
    assert generator.calls[0]["previous_error"] is None
    assert recorder.attempt_failures == []
    assert [event.defect_code for event in recorder.defect_events] == [
        _DEFECT_CITED_REFS_RECOMPUTED
    ]


@pytest.mark.asyncio
async def test_extra_declared_cited_refs_are_replaced_by_answer_markers() -> None:
    generator = FakeGenerator(
        [
            _raw(
                answer="根拠 1 だけを使っています。[[1]]",
                cited_refs=["1", "2"],
            )
        ]
    )
    recorder = FakeAnswerSynthesisAuditRecorder()

    draft = await _answer(
        generator,
        recorder=recorder,
        evidence=[_evidence("1"), _evidence("2")],
    )

    assert draft.cited_refs == ["1"]
    assert [event.defect_code for event in recorder.defect_events] == [
        _DEFECT_CITED_REFS_RECOMPUTED
    ]


@pytest.mark.asyncio
async def test_completes_insufficient_missing_aspects_and_records_defect() -> None:
    generator = FakeGenerator(
        [
            _raw(
                sufficiency="insufficient",
                answer="根拠の範囲では断定できません。[[1]]",
                cited_refs=["1"],
                missing_aspects=[],
            )
        ]
    )
    recorder = FakeAnswerSynthesisAuditRecorder()

    draft = await _answer(generator, recorder=recorder)

    assert draft.sufficiency == "insufficient"
    assert draft.answer == "根拠の範囲では断定できません。[[1]]"
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
                answer="一部だけ確認できます。[[1]]",
                cited_refs=["1", "", "1", "  ", "1"],
                missing_aspects=["", "会社側の一次情報", "会社側の一次情報", "\n"],
            )
        ]
    )
    recorder = FakeAnswerSynthesisAuditRecorder()

    draft = await _answer(generator, recorder=recorder)

    assert draft.cited_refs == ["1"]
    assert draft.missing_aspects == ["会社側の一次情報"]
    assert recorder.defect_events
    assert recorder.final_events[0].defect_count == len(recorder.defect_events)


@pytest.mark.asyncio
async def test_answered_without_marker_retries_once_with_previous_error() -> None:
    repaired = _raw(
        sufficiency="answered",
        answer="修正後は根拠を引用しています。[[1]]",
        cited_refs=["1"],
    )
    generator = FakeGenerator(
        [
            _raw(
                sufficiency="answered",
                answer="引用がありません。",
                cited_refs=["1"],
            ),
            repaired,
        ]
    )
    recorder = FakeAnswerSynthesisAuditRecorder()

    draft = await _answer(generator, recorder=recorder)

    assert draft.answer == "修正後は根拠を引用しています。[[1]]"
    assert [call["previous_error"] for call in generator.calls][0] is None
    assert "citation marker" in generator.calls[1]["previous_error"]
    assert [event.attempt_number for event in recorder.attempt_failures] == [1]
    assert recorder.final_events[0].attempt_count == 2
    assert recorder.final_events[0].retry_used is True


@pytest.mark.asyncio
async def test_persistent_noncompletable_defect_falls_back_to_valid_insufficient(
    capfire: CaptureLogfire,
) -> None:
    generator = FakeGenerator(
        [
            _raw(sufficiency="answered", answer="引用がありません。", cited_refs=["1"]),
            _raw(
                sufficiency="answered",
                answer="まだ引用がありません。",
                cited_refs=["1"],
            ),
        ]
    )
    recorder = FakeAnswerSynthesisAuditRecorder()

    draft = await _answer(generator, recorder=recorder)

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
                answer="存在しない根拠を引用しています。[[2]]",
                cited_refs=["2"],
            ),
            _raw(
                sufficiency="answered",
                answer="実在する根拠を引用しています。[[1]]",
                cited_refs=["1"],
            ),
        ]
    )
    recorder = FakeAnswerSynthesisAuditRecorder()

    draft = await _answer(generator, recorder=recorder)

    assert draft.cited_refs == ["1"]
    assert "[[2]]" in generator.calls[1]["previous_error"]
    assert recorder.attempt_failures[0].failure_kind == "ai_response_invalid"


@pytest.mark.asyncio
async def test_persistent_unknown_marker_falls_back_to_valid_insufficient() -> None:
    generator = FakeGenerator(
        [
            _raw(
                sufficiency="answered",
                answer="存在しない根拠を引用しています。[[9]]",
                cited_refs=["9"],
            ),
            _raw(
                sufficiency="answered",
                answer="まだ存在しない根拠を引用しています。[[9]]",
                cited_refs=["9"],
            ),
        ]
    )

    draft = await _answer(generator)

    assert draft.sufficiency == "insufficient"
    assert draft.cited_refs == []
    assert draft.missing_aspects
    assert "[[9]]" in generator.calls[1]["previous_error"]


@pytest.mark.asyncio
async def test_empty_evidence_answered_citation_falls_back_insufficient() -> None:
    generator = FakeGenerator(
        [
            _raw(
                sufficiency="answered",
                answer="根拠がないのに引用しています。[[1]]",
                cited_refs=["1"],
            ),
            _raw(
                sufficiency="answered",
                answer="まだ根拠がないのに引用しています。[[1]]",
                cited_refs=["1"],
            ),
        ]
    )

    draft = await _answer(generator, evidence=[])

    assert draft.sufficiency == "insufficient"
    assert draft.cited_refs == []
    assert draft.missing_aspects
    assert len(generator.calls) == 2
    assert "[[1]]" in generator.calls[1]["previous_error"]


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

    draft = await _answer(generator, evidence=[])

    assert draft.sufficiency == "insufficient"
    assert draft.cited_refs == []
    assert draft.missing_aspects == ["引用できる検索根拠"]
    assert len(generator.calls) == 1


@pytest.mark.asyncio
async def test_marker_parse_boundaries_use_double_bracket_digits_only() -> None:
    generator = FakeGenerator(
        [
            _raw(
                answer=(
                    "連続 marker を使います。[[1]][[2]] "
                    "文中 marker も引用として扱います [[2]]。"
                    "単括弧 [1] は marker ではありません。"
                ),
                cited_refs=["1", "2"],
            )
        ]
    )
    recorder = FakeAnswerSynthesisAuditRecorder()

    draft = await _answer(
        generator,
        recorder=recorder,
        evidence=[_evidence("1"), _evidence("2")],
    )

    assert draft.cited_refs == ["1", "2"]
    assert recorder.defect_events == []


@pytest.mark.asyncio
async def test_repeated_markers_are_deduplicated_without_defect() -> None:
    generator = FakeGenerator(
        [
            _raw(
                answer="同じ根拠を複数回引用します。[[1]] 別の文でも使います。[[1]]",
                cited_refs=["1"],
            )
        ]
    )
    recorder = FakeAnswerSynthesisAuditRecorder()

    draft = await _answer(generator, recorder=recorder)

    assert draft.cited_refs == ["1"]
    assert recorder.defect_events == []


@pytest.mark.asyncio
async def test_insufficient_with_marker_keeps_partial_citations() -> None:
    generator = FakeGenerator(
        [
            _raw(
                sufficiency="insufficient",
                answer="根拠の範囲では需要は強いです。[[1]]",
                cited_refs=[],
                missing_aspects=["会社側の一次情報"],
            )
        ]
    )
    recorder = FakeAnswerSynthesisAuditRecorder()

    draft = await _answer(generator, recorder=recorder)

    assert draft.sufficiency == "insufficient"
    assert draft.cited_refs == ["1"]
    assert draft.missing_aspects == ["会社側の一次情報"]
    assert [event.defect_code for event in recorder.defect_events] == [
        _DEFECT_CITED_REFS_RECOMPUTED
    ]


@pytest.mark.asyncio
async def test_provider_error_falls_back_without_retry() -> None:
    generator = FakeGenerator([AIProviderNetworkError()])
    recorder = FakeAnswerSynthesisAuditRecorder()

    draft = await _answer(generator, recorder=recorder)

    assert draft.sufficiency == "insufficient"
    assert len(generator.calls) == 1
    assert recorder.attempt_failures[0].request_retry_disposition is (
        RequestRetryDisposition.DO_NOT_RETRY_IN_REQUEST
    )
    assert recorder.final_events[0].retry_used is False


@pytest.mark.asyncio
async def test_response_envelope_error_retries_once_with_previous_error() -> None:
    invalid = EvidenceAnswerDraftGenerationInvalidError("response_not_json")
    generator = FakeGenerator([invalid, _raw(cited_refs=["1"])])
    recorder = FakeAnswerSynthesisAuditRecorder()

    draft = await _answer(generator, recorder=recorder)

    assert draft.sufficiency == "answered"
    assert [call["previous_error"] for call in generator.calls] == [
        None,
        "response_not_json",
    ]
    assert recorder.attempt_failures[0].code == "response_not_json"
    assert recorder.final_events[0].retry_used is True


@pytest.mark.asyncio
async def test_unexpected_exception_propagates_without_fallback(
    capfire: CaptureLogfire,
) -> None:
    generator = FakeGenerator([RuntimeError("bug in generator")])
    recorder = FakeAnswerSynthesisAuditRecorder()

    with pytest.raises(RuntimeError, match="bug in generator"):
        await _answer(generator, recorder=recorder)

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
                answer="一部だけ確認できます。[[1]]",
                cited_refs=["1"],
                missing_aspects=[],
            )
        ]
    )

    draft = await EvidenceAnswerFlow(
        generator=generator,
        audit_recorder=RaisingAnswerSynthesisAuditRecorder(),
    ).answer(
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

    await _answer(generator)

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


@pytest.mark.asyncio
async def test_stream_displays_only_filtered_root_answer_for_generation_one() -> None:
    raw_json = (
        '{"sufficiency":"answered","metadata":{"answer":"NESTED_SECRET"},'
        '"answer":"  結論 [[1]] と [[x]] は残す。  ",'
        '"cited_refs":["1"],"missing_aspects":[]}'
    )
    generator = FakeGenerator(
        [
            [
                raw_json[:72],
                raw_json[72:88],
                raw_json[88:91],
                raw_json[91:],
            ]
        ]
    )
    reporter = RecordingDeltaReporter()

    draft = await _answer(generator, delta_reporter=reporter)

    visible = "".join(text for _, text in reporter.appended)
    assert visible == "結論  と [[x]] は残す。"
    assert visible == draft.answer.replace("[[1]]", "").strip()
    assert "NESTED_SECRET" not in visible
    assert "sufficiency" not in visible
    assert "cited_refs" not in visible
    assert "missing_aspects" not in visible
    assert generator.calls == [
        {
            "question": "NVIDIA の直近発表は投資判断に重要？",
            "evidence": [_evidence()],
            "as_of": _as_of(),
            "target_time_window": "今日",
            "user_intent": "",
            "prior_coverage": "",
            "user_activity_context": "",
            "previous_error": None,
        }
    ]
    assert generator.streams[0].closed is True
    assert reporter.finished == [1]
    assert reporter.aborted == []
    assert reporter.reset_generations == []


@pytest.mark.asyncio
async def test_insufficient_root_answer_is_streamed_normally() -> None:
    generator = FakeGenerator(
        [
            _raw(
                sufficiency="insufficient",
                answer="根拠の範囲では一部だけ確認できます。[[1]]",
                cited_refs=["1"],
                missing_aspects=["会社側の一次情報"],
            )
        ]
    )
    reporter = RecordingDeltaReporter()

    draft = await _answer(generator, delta_reporter=reporter)

    assert draft.sufficiency == "insufficient"
    assert "".join(text for _, text in reporter.appended) == (
        "根拠の範囲では一部だけ確認できます。"
    )
    assert reporter.finished == [1]


@pytest.mark.parametrize(
    "invalid_json",
    [
        "not json",
        "[]",
        (
            '{"sufficiency":"answered","answer":"first",'
            '"answer":"second","cited_refs":["1"],"missing_aspects":[]}'
        ),
        (
            '{"sufficiency":"answered","answer":"schema invalid",'
            '"cited_refs":"1","missing_aspects":[]}'
        ),
    ],
    ids=["invalid-json", "non-object", "duplicate-top-level-key", "schema"],
)
@pytest.mark.asyncio
async def test_final_json_boundary_retries_then_falls_back_with_typed_audit(
    invalid_json: str,
) -> None:
    generator = FakeGenerator([invalid_json, invalid_json])
    recorder = FakeAnswerSynthesisAuditRecorder()

    draft = await _answer(generator, recorder=recorder)

    assert draft.sufficiency == "insufficient"
    assert len(generator.calls) == 2
    assert all(stream.closed for stream in generator.streams)
    assert [event.attempt_number for event in recorder.attempt_failures] == [1, 2]
    assert all(
        event.failure_kind == "ai_response_invalid"
        for event in recorder.attempt_failures
    )
    assert {event.request_retry_disposition for event in recorder.attempt_failures} == {
        RequestRetryDisposition.RETRY_IN_REQUEST
    }
    assert recorder.final_events[0].outcome_code is (
        AnswerSynthesisOutcomeCode.FALLBACK_USED
    )


@pytest.mark.asyncio
async def test_retry_aborts_then_resets_before_generation_two_delta() -> None:
    generator = FakeGenerator(
        [
            _raw(answer="引用がありません。", cited_refs=["1"]),
            _raw(answer="修正後は引用します。[[1]]", cited_refs=["1"]),
        ]
    )
    recorder = FakeAnswerSynthesisAuditRecorder()
    reporter = RecordingDeltaReporter()

    draft = await _answer(
        generator,
        recorder=recorder,
        delta_reporter=reporter,
    )

    assert draft.answer == "修正後は引用します。[[1]]"
    assert reporter.aborted == [1]
    assert reporter.reset_generations == [2]
    assert reporter.finished == [2]
    operations = _operation_names(reporter)
    assert operations.index(("abort", 1)) < operations.index(("reset", 2))
    first_generation_two_append = operations.index(("append", 2))
    assert operations.index(("reset", 2)) < first_generation_two_append
    assert "citation marker" in generator.calls[1]["previous_error"]
    assert all(stream.closed for stream in generator.streams)
    assert recorder.final_events[0].attempt_count == 2
    assert recorder.final_events[0].retry_used is True


@pytest.mark.asyncio
async def test_retry_resets_even_when_failed_generation_had_no_visible_delta() -> None:
    generator = FakeGenerator(
        [
            "not json",
            _raw(answer="再試行は引用します。[[1]]", cited_refs=["1"]),
        ]
    )
    reporter = RecordingDeltaReporter()

    draft = await _answer(generator, delta_reporter=reporter)

    assert draft.answer == "再試行は引用します。[[1]]"
    assert all(generation != 1 for generation, _ in reporter.appended)
    assert reporter.aborted == [1]
    assert reporter.reset_generations == [2]
    assert reporter.finished == [2]


@pytest.mark.asyncio
async def test_two_retryable_failures_reset_to_generation_three_fallback() -> None:
    generator = FakeGenerator(["not json", "still not json"])
    recorder = FakeAnswerSynthesisAuditRecorder()
    reporter = RecordingDeltaReporter()

    draft = await _answer(
        generator,
        recorder=recorder,
        delta_reporter=reporter,
    )

    assert draft.sufficiency == "insufficient"
    assert reporter.aborted == [1, 2]
    assert reporter.reset_generations == [2, 3]
    assert reporter.finished == [3]
    assert {generation for generation, _ in reporter.appended} == {3}
    assert (
        "".join(text for generation, text in reporter.appended if generation == 3)
        == draft.answer
    )
    operations = _operation_names(reporter)
    assert operations.index(("abort", 1)) < operations.index(("reset", 2))
    assert operations.index(("abort", 2)) < operations.index(("reset", 3))
    assert operations.index(("reset", 3)) < operations.index(("append", 3))
    assert recorder.final_events[0].fallback_used is True
    assert recorder.final_events[0].retry_used is True


@pytest.mark.asyncio
async def test_provider_error_resets_once_then_streams_generation_two_fallback() -> (
    None
):
    generator = FakeGenerator([AIProviderNetworkError()])
    reporter = RecordingDeltaReporter()

    draft = await _answer(generator, delta_reporter=reporter)

    assert draft.sufficiency == "insufficient"
    assert reporter.aborted == [1]
    assert reporter.reset_generations == [2]
    assert reporter.finished == [2]
    assert {generation for generation, _ in reporter.appended} == {2}
    assert (
        "".join(text for generation, text in reporter.appended if generation == 2)
        == draft.answer
    )
    assert generator.streams[0].closed is True


@pytest.mark.asyncio
async def test_all_reporter_failures_do_not_change_retry_result() -> None:
    generator = FakeGenerator(
        [
            _raw(answer="引用がありません。", cited_refs=["1"]),
            _raw(answer="修正後の回答です。[[1]]", cited_refs=["1"]),
        ]
    )
    reporter = RecordingDeltaReporter(
        fail_on=frozenset({"append", "finish", "abort", "reset"})
    )

    draft = await _answer(generator, delta_reporter=reporter)

    assert draft.answer == "修正後の回答です。[[1]]"
    assert reporter.aborted == [1]
    assert reporter.reset_generations == [2]
    assert reporter.finished == [2]
    assert any(generation == 2 for generation, _ in reporter.appended)


@pytest.mark.parametrize("with_failing_reporter", [False, True])
@pytest.mark.asyncio
async def test_reporter_is_not_part_of_final_draft_correctness(
    with_failing_reporter: bool,
) -> None:
    generator = FakeGenerator([_raw(cited_refs=["1"])])
    reporter = (
        RecordingDeltaReporter(fail_on=frozenset({"append", "finish"}))
        if with_failing_reporter
        else None
    )

    draft = await _answer(generator, delta_reporter=reporter)

    assert draft == EvidenceAnswerDraft(
        sufficiency="answered",
        answer="根拠から確認できます。[[1]]",
        cited_refs=["1"],
    )


@pytest.mark.asyncio
async def test_continuation_false_before_provider_start_is_routine_stop() -> None:
    stopped_type = _answer_generation_stopped_type()
    assert not issubclass(stopped_type, AIProviderError)
    generator = FakeGenerator([_raw()])
    recorder = FakeAnswerSynthesisAuditRecorder()
    reporter = RecordingDeltaReporter()

    with pytest.raises(stopped_type):
        await _answer(
            generator,
            recorder=recorder,
            delta_reporter=reporter,
            continuation=SequenceContinuation([False]),
        )

    assert generator.calls == []
    assert generator.streams == []
    assert reporter.aborted == [1]
    assert reporter.appended == []
    assert reporter.finished == []
    assert recorder.attempt_failures == []
    assert recorder.final_events == []


@pytest.mark.asyncio
async def test_continuation_false_mid_stream_closes_and_aborts() -> None:
    stopped_type = _answer_generation_stopped_type()
    raw_json = _raw_json(_raw(answer="表示済み本文と非表示本文。[[1]]"))
    answer_start = raw_json.index("表示済み本文") + len("表示済み本文")
    generator = FakeGenerator([[raw_json[:answer_start], raw_json[answer_start:]]])
    recorder = FakeAnswerSynthesisAuditRecorder()
    reporter = RecordingDeltaReporter()
    continuation = SequenceContinuation([True, True, False])

    with pytest.raises(stopped_type):
        await _answer(
            generator,
            recorder=recorder,
            delta_reporter=reporter,
            continuation=continuation,
        )

    assert "".join(text for _, text in reporter.appended) == "表示済み本文"
    assert reporter.aborted == [1]
    assert reporter.finished == []
    assert generator.streams[0].closed is True
    assert recorder.final_events == []


@pytest.mark.asyncio
async def test_continuation_false_at_eof_stops_before_final_parse_and_audit() -> None:
    stopped_type = _answer_generation_stopped_type()
    generator = FakeGenerator([_raw()])
    recorder = FakeAnswerSynthesisAuditRecorder()
    reporter = RecordingDeltaReporter()
    continuation = SequenceContinuation([True, True, False])

    with pytest.raises(stopped_type):
        await _answer(
            generator,
            recorder=recorder,
            delta_reporter=reporter,
            continuation=continuation,
        )

    assert reporter.aborted == [1]
    assert reporter.finished == []
    assert reporter.reset_generations == []
    assert generator.streams[0].closed is True
    assert recorder.attempt_failures == []
    assert recorder.final_events == []


@pytest.mark.asyncio
async def test_continuation_false_after_provider_error_stops_before_fallback() -> None:
    stopped_type = _answer_generation_stopped_type()
    generator = FakeGenerator([AIProviderNetworkError()])
    recorder = FakeAnswerSynthesisAuditRecorder()
    reporter = RecordingDeltaReporter()

    with pytest.raises(stopped_type):
        await _answer(
            generator,
            recorder=recorder,
            delta_reporter=reporter,
            continuation=SequenceContinuation([True, False]),
        )

    assert generator.streams[0].closed is True
    assert reporter.aborted == [1]
    assert reporter.reset_generations == []
    assert reporter.appended == []
    assert reporter.finished == []
    assert recorder.final_events == []
