"""Evidence answer flow tests(S4: response_schema=Noneのplain text契約)。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from importlib import import_module
from typing import Any

import pytest
from logfire.testing import CaptureLogfire
from pydantic import ValidationError

from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.evidence_answer.contract import EvidenceAnswerDraft
from app.agent.answering.evidence_answer.evidence import AnswerEvidenceItem
from app.agent.answering.evidence_answer.flow import EvidenceAnswerFlow
from app.agent.contract import ExternalUrlSource
from app.agent.planning.contract import TargetTimeWindow
from app.agent.question_context.contract import AnswerRequirement, QuestionContext
from app.analysis.ai_provider_errors import (
    AIProviderError,
    AIProviderNetworkError,
    AIProviderOutputTruncatedError,
)
from app.analysis.gemini_error_translator import GeminiStateReason
from tests.logfire._metric_helpers import (
    collected_metrics,
    counter_attribute_key_sets,
    sum_counter_for_result,
)
from tests.logfire._span_helpers import (
    domain_attr_keys,
    exception_event,
    one_span_named,
)

_SYNTHESIS_OUTCOME_METRIC = "vector.agent.answer_synthesis.outcome"
_PHASE_SPAN_NAME = "agent_phase"


def _as_of() -> datetime:
    return datetime(2026, 7, 7, 9, 0, tzinfo=UTC)


def _request(
    *,
    content_requirements: list[AnswerRequirement] | None = None,
    response_requirements: list[AnswerRequirement] | None = None,
) -> AnsweringRequest:
    return AnsweringRequest(
        context=QuestionContext(
            standalone_question="NVIDIA の直近発表は投資判断に重要？",
            content_requirements=(
                [
                    AnswerRequirement(
                        requirement_id="c1",
                        description="投資判断への影響を説明する",
                    )
                ]
                if content_requirements is None
                else content_requirements
            ),
            response_requirements=(
                [
                    AnswerRequirement(
                        requirement_id="p1",
                        description="根拠付きで詳しく回答する",
                    )
                ]
                if response_requirements is None
                else response_requirements
            ),
            relevant_prior_coverage="前回は発表内容を説明済み",
            active_goal="投資判断を進める",
        ),
        as_of=_as_of(),
    )


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


def _truncated_error() -> AIProviderOutputTruncatedError:
    """S1 runtimeが実際に送出する形 (reason付き) を再現する。"""
    return AIProviderOutputTruncatedError(
        reason=GeminiStateReason.OUTPUT_TOKEN_LIMIT_REACHED
    )


def _evidence_answer_unavailable_type() -> Any:
    contract = import_module("app.agent.answering.evidence_answer.contract")
    unavailable_type = getattr(contract, "EvidenceAnswerUnavailable", None)
    if unavailable_type is None:
        pytest.fail(
            "S3: app.agent.answering.evidence_answer.contract must define "
            "EvidenceAnswerUnavailable"
        )
    return unavailable_type


def _expected_draft(*, answer: str, cited_refs: list[str]) -> Any:
    """S4: EvidenceAnswerDraftはanswerとcited_refsだけを持つ

    (sufficiency/missing_aspects/unfulfilled_requirement_idsは撤去される)。
    現行モデルはまだsufficiencyを必須で要求するため、比較用の期待値構築を
    ガードしてassertion failureのredにする。
    """
    try:
        return EvidenceAnswerDraft(answer=answer, cited_refs=cited_refs)
    except ValidationError:
        pytest.fail(
            "S4: EvidenceAnswerDraft must accept only "
            "(answer: NonBlankText, cited_refs: list[str])"
        )


def _operation_names(reporter: RecordingDeltaReporter) -> list[tuple[str, int]]:
    return [(name, generation) for name, generation, _ in reporter.operations]


StreamOutcome = str | Sequence[str] | Exception


class FakeEvidenceAnswerStream:
    def __init__(
        self,
        outcome: StreamOutcome,
        *,
        attempt_number: int,
        events: list[str],
    ) -> None:
        if isinstance(outcome, Exception):
            self._items: list[str | Exception] = [outcome]
        elif isinstance(outcome, str):
            self._items = [outcome]
        else:
            self._items = list(outcome)
        self._attempt_number = attempt_number
        self._events = events
        self.closed = False
        self.close_calls = 0

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
        self.close_calls += 1
        self.closed = True
        self._events.append(f"stream_close:{self._attempt_number}")


class FakeGenerator:
    def __init__(self, outcomes: Sequence[StreamOutcome]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []
        self.inputs: list[Any] = []
        self.streams: list[FakeEvidenceAnswerStream] = []
        self.scope_enters = 0
        self.scope_exits = 0
        self.events: list[str] = []

    def invoke_stream(
        self,
        agent: object,
        input: object,
        *,
        attempt_number: int,
    ) -> AsyncIterator[str]:
        self.events.append(f"invoke:{attempt_number}")
        self.inputs.append(input)
        self.calls.append(
            {
                "agent": agent,
                "request": input.request,
                "evidence": input.evidence,
                "target_time_window": input.target_time_window,
                "previous_error": input.previous_error,
                "attempt_number": attempt_number,
            }
        )
        if not self._outcomes:
            pytest.fail(
                f"S4: unexpected extra attempt (attempt_number={attempt_number}). "
                "The flow retried more than this test staged fragments for; "
                "the current implementation may still parse the stream as "
                "structured JSON instead of plain text."
            )
        outcome = self._outcomes.pop(0)
        stream = FakeEvidenceAnswerStream(
            outcome,
            attempt_number=attempt_number,
            events=self.events,
        )
        self.streams.append(stream)
        return stream

    @asynccontextmanager
    async def activate(self) -> AsyncIterator[FakeGenerator]:
        self.scope_enters += 1
        self.events.append("scope_enter")
        try:
            yield self
        finally:
            self.scope_exits += 1
            self.events.append("scope_exit")


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


def _evidence_answer_agent() -> object:
    agent_module = import_module("app.agent.answering.evidence_answer.agent")
    agent = getattr(agent_module, "EVIDENCE_ANSWER_AGENT", None)
    assert agent is not None, "EVIDENCE_ANSWER_AGENT が未実装です"
    return agent


async def _answer(
    generator: FakeGenerator,
    *,
    evidence: list[AnswerEvidenceItem] | None = None,
    delta_reporter: RecordingDeltaReporter | None = None,
    continuation: SequenceContinuation | None = None,
    request: AnsweringRequest | None = None,
) -> Any:
    flow_kwargs: dict[str, Any] = {
        "agent": _evidence_answer_agent(),
        "runtime_scope_factory": generator.activate,
    }
    if delta_reporter is not None:
        flow_kwargs["delta_reporter"] = delta_reporter
    if continuation is not None:
        flow_kwargs["continuation"] = continuation
    try:
        return await EvidenceAnswerFlow(**flow_kwargs).answer(
            request=_request() if request is None else request,
            evidence=[_evidence()] if evidence is None else evidence,
            target_time_window=TargetTimeWindow(kind="today"),
            review_missing=(),
        )
    except TypeError:
        pytest.fail(
            "S5追補: EvidenceAnswerFlow.answer must accept a required "
            "review_missing keyword argument (tuple[str, ...])"
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
async def test_valid_answer_with_marker_returns_draft_without_retry(
    capfire: CaptureLogfire,
) -> None:
    generator = FakeGenerator(["根拠から確認できます。[[1]]"])

    draft = await _answer(generator)

    assert draft == _expected_draft(
        answer="根拠から確認できます。[[1]]",
        cited_refs=["1"],
    )
    assert generator.calls[0]["previous_error"] is None
    assert generator.calls[0]["agent"] is _evidence_answer_agent()
    assert generator.calls[0]["evidence"] == (_evidence(),)
    assert generator.calls[0]["attempt_number"] == 1
    assert generator.streams[0].close_calls == 1
    assert (generator.scope_enters, generator.scope_exits) == (1, 1)
    assert generator.events == [
        "scope_enter",
        "invoke:1",
        "stream_close:1",
        "scope_exit",
    ]
    phase = one_span_named(capfire, _PHASE_SPAN_NAME)
    assert domain_attr_keys(phase["attributes"]) == {"phase", "agent_name"}
    assert phase["attributes"]["phase"] == "evidence_answer"
    assert phase["attributes"]["agent_name"] == "evidence_answer"
    assert exception_event(phase) is None


@pytest.mark.asyncio
async def test_fragments_flow_unchanged_and_concatenate_into_the_answer() -> None:
    """条件2・11: fragmentが加工されずlive draftへ流れ、連結結果が本文になる。

    本文のmarkerは除去されず保持されるが(cited_refsの算出元)、live表示は
    AnswerVisibleTextFilterにより現行どおりmarkerを除く。
    """
    generator = FakeGenerator([["根拠から", "確認できます。", "[[1]]"]])
    reporter = RecordingDeltaReporter()

    draft = await _answer(generator, delta_reporter=reporter)

    assert draft == _expected_draft(
        answer="根拠から確認できます。[[1]]",
        cited_refs=["1"],
    )
    assert "".join(text for _, text in reporter.appended) == "根拠から確認できます。"
    assert {generation for generation, _ in reporter.appended} == {1}
    assert reporter.finished == [1]
    assert reporter.aborted == []


@pytest.mark.asyncio
async def test_json_shaped_body_is_treated_as_literal_answer_text() -> None:
    """条件3: JSONを模した本文がそのまま本文として扱われ、field抽出が起きない。"""
    json_like_answer = '{"answer": "本文", "cited_refs": ["1"]}確認できます。[[1]]'
    generator = FakeGenerator([json_like_answer])

    draft = await _answer(generator)

    assert draft.answer == json_like_answer
    assert draft.cited_refs == ["1"]


@pytest.mark.asyncio
async def test_derives_cited_refs_from_answer_markers_in_first_use_order() -> None:
    """条件5: 本文の[[1]][[2]]からcited_refsが初出順・重複排除で算出される
    (marker解釈の網羅ケースはtest_validation.pyが正本、ここは配線の確認)。
    """
    generator = FakeGenerator(
        ["根拠 2 と根拠 1 を確認できます。[[2]][[1]] 再度 [[2]]。"]
    )

    draft = await _answer(generator, evidence=[_evidence("1"), _evidence("2")])

    assert draft.cited_refs == ["2", "1"]


@pytest.mark.asyncio
async def test_unknown_citation_ref_retries_then_falls_back_to_unavailable(
    capfire: CaptureLogfire,
) -> None:
    """条件6: evidenceに存在しないrefを本文が参照したdraftは不正として retry され、

    2回目も解消しなければ生成不能へ落ちる。
    """
    generator = FakeGenerator(
        [
            "存在しない根拠を引用しています。[[9]]",
            "まだ存在しない根拠を引用しています。[[9]]",
        ]
    )

    outcome = await _answer(generator)

    assert len(generator.calls) == 2
    assert "[[9]]" in generator.calls[1]["previous_error"]
    assert isinstance(outcome, _evidence_answer_unavailable_type())
    assert getattr(outcome, "failure_code", None) == "answer_synthesis_draft_invalid"
    metrics = collected_metrics(capfire)
    assert _metric_attributes(metrics, _SYNTHESIS_OUTCOME_METRIC) == [
        {
            "result": "fallback",
            "retry_used": True,
            "fallback_used": True,
            "failure_code": "answer_synthesis_draft_invalid",
        }
    ]


@pytest.mark.asyncio
async def test_no_marker_with_evidence_retries_then_falls_back_to_unavailable() -> None:
    """条件7: evidenceが非空でmarkerが無い本文は不正として retry され、

    2回目もmarker無しなら生成不能(EvidenceAnswerUnavailable)へ落ちる。
    """
    generator = FakeGenerator(["引用がありません。", "まだ引用がありません。"])

    outcome = await _answer(generator)

    assert len(generator.calls) == 2
    assert isinstance(outcome, _evidence_answer_unavailable_type())
    assert getattr(outcome, "failure_code", None) == "answer_synthesis_draft_invalid"


@pytest.mark.asyncio
async def test_empty_evidence_without_marker_is_valid_with_empty_cited_refs() -> None:
    """条件8: evidenceが空でmarkerが無い本文は正常に成立し、cited_refsが空になる

    (sourcesがcited_refsだけから組み立てられるため、これがsources空を担保する)。
    """
    answer = (
        "検索で引用できる根拠は見つかりませんでした。"
        "一般論では参考程度に考えてください。"
    )
    generator = FakeGenerator([answer])

    draft = await _answer(generator, evidence=[])

    assert draft == _expected_draft(answer=answer, cited_refs=[])
    assert len(generator.calls) == 1


@pytest.mark.asyncio
async def test_empty_evidence_with_marker_falls_back_to_unavailable() -> None:
    """条件9: evidenceが空でmarkerがある本文は不正(回帰ガード)。"""
    generator = FakeGenerator(
        [
            "根拠がないのに引用しています。[[1]]",
            "まだ根拠がないのに引用しています。[[1]]",
        ]
    )

    outcome = await _answer(generator, evidence=[])

    assert len(generator.calls) == 2
    assert isinstance(outcome, _evidence_answer_unavailable_type())
    assert "[[1]]" in generator.calls[1]["previous_error"]


@pytest.mark.asyncio
async def test_blank_answer_falls_back_with_draft_invalid_not_pydantic_code() -> None:
    """条件4: 空白のみの本文はdraft不正として扱われ、failure_codeは

    answer_synthesis_pydantic_validation_failedではなくdraft不正側になる。
    """
    generator = FakeGenerator(["   ", "\n\t"])

    outcome = await _answer(generator)

    assert len(generator.calls) == 2
    assert getattr(outcome, "failure_code", None) == "answer_synthesis_draft_invalid"


@pytest.mark.asyncio
async def test_generation_unavailable_is_a_distinct_type_with_only_failure_code() -> (
    None
):
    """生成不能は回答draftと別の型で表し、failure_codeだけを持つ。"""
    unavailable_type = _evidence_answer_unavailable_type()
    generator = FakeGenerator([AIProviderNetworkError()])

    outcome = await _answer(generator)

    assert isinstance(outcome, unavailable_type)
    assert not isinstance(outcome, EvidenceAnswerDraft)
    assert set(type(outcome).model_fields) == {"failure_code"}
    assert outcome.failure_code == "ai_error_network"


@pytest.mark.asyncio
async def test_provider_error_falls_back_without_retry(
    capfire: CaptureLogfire,
) -> None:
    generator = FakeGenerator([AIProviderNetworkError()])

    outcome = await _answer(generator)

    assert getattr(outcome, "failure_code", None) == "ai_error_network"
    assert len(generator.calls) == 1
    metrics = collected_metrics(capfire)
    assert _metric_attributes(metrics, _SYNTHESIS_OUTCOME_METRIC) == [
        {
            "result": "fallback",
            "retry_used": False,
            "fallback_used": True,
            "failure_code": "ai_error_network",
        }
    ]


@pytest.mark.asyncio
async def test_blank_response_retries_once_with_previous_error(
    capfire: CaptureLogfire,
) -> None:
    generator = FakeGenerator([" \n\t", "修正後は根拠を引用しています。[[1]]"])

    draft = await _answer(generator)

    assert draft == _expected_draft(
        answer="修正後は根拠を引用しています。[[1]]",
        cited_refs=["1"],
    )
    assert [call["previous_error"] for call in generator.calls][0] is None
    assert generator.calls[1]["previous_error"] is not None
    metrics = collected_metrics(capfire)
    assert _metric_attributes(metrics, _SYNTHESIS_OUTCOME_METRIC) == [
        {
            "result": "synthesized",
            "retry_used": True,
            "fallback_used": False,
            "failure_code": "none",
        }
    ]


@pytest.mark.asyncio
async def test_unexpected_exception_propagates_without_fallback(
    capfire: CaptureLogfire,
) -> None:
    generator = FakeGenerator([RuntimeError("bug in generator")])

    with pytest.raises(RuntimeError, match="bug in generator"):
        await _answer(generator)

    assert len(generator.calls) == 1
    metrics = collected_metrics(capfire)
    assert _metric_attributes(metrics, _SYNTHESIS_OUTCOME_METRIC) == []


@pytest.mark.asyncio
async def test_runtime_scope_activation_failure_is_not_attempt_fallback(
    capfire: CaptureLogfire,
) -> None:
    failure = RuntimeError("runtime scope unavailable")
    reporter = RecordingDeltaReporter()

    @asynccontextmanager
    async def failing_scope() -> AsyncIterator[object]:
        raise failure
        yield object()

    flow = EvidenceAnswerFlow(
        agent=_evidence_answer_agent(),
        runtime_scope_factory=failing_scope,
        delta_reporter=reporter,
    )

    with pytest.raises(RuntimeError) as exc_info:
        try:
            await flow.answer(
                request=_request(),
                evidence=[_evidence()],
                target_time_window=TargetTimeWindow(kind="today"),
                review_missing=(),
            )
        except TypeError:
            pytest.fail(
                "S5追補: EvidenceAnswerFlow.answer must accept a required "
                "review_missing keyword argument (tuple[str, ...])"
            )

    phase = one_span_named(capfire, _PHASE_SPAN_NAME)
    assert exc_info.value is failure
    assert reporter.operations == []
    assert (
        _metric_attributes(collected_metrics(capfire), _SYNTHESIS_OUTCOME_METRIC) == []
    )
    assert exception_event(phase) is not None
    assert domain_attr_keys(phase["attributes"]) == {"phase", "agent_name"}


@pytest.mark.asyncio
async def test_outcome_metric_records_synthesized_once(
    capfire: CaptureLogfire,
) -> None:
    generator = FakeGenerator(["根拠から確認できます。[[1]]"])

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
            "fallback_used": False,
            "failure_code": "none",
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcomes",
    [
        ["根拠から確認できます。[[1]]"],
        [AIProviderNetworkError()],
    ],
    ids=["synthesized", "fallback"],
)
async def test_synthesis_outcome_metric_key_set_excludes_status(
    outcomes: Sequence[StreamOutcome],
    capfire: CaptureLogfire,
) -> None:
    """statusラベルを廃止し、成功時・生成不能時ともkey集合が固定4つになる。"""
    generator = FakeGenerator(outcomes)

    await _answer(generator)

    metrics = collected_metrics(capfire)
    key_sets = counter_attribute_key_sets(metrics, _SYNTHESIS_OUTCOME_METRIC)
    assert key_sets == [{"result", "retry_used", "fallback_used", "failure_code"}]


@pytest.mark.asyncio
async def test_retry_aborts_then_resets_before_generation_two_delta() -> None:
    generator = FakeGenerator(["引用がありません。", "修正後は引用します。[[1]]"])
    reporter = RecordingDeltaReporter()

    draft = await _answer(generator, delta_reporter=reporter)

    assert draft == _expected_draft(
        answer="修正後は引用します。[[1]]",
        cited_refs=["1"],
    )
    assert reporter.aborted == [1]
    assert reporter.reset_generations == [2]
    assert reporter.finished == [2]
    operations = _operation_names(reporter)
    assert operations.index(("abort", 1)) < operations.index(("reset", 2))
    first_generation_two_append = operations.index(("append", 2))
    assert operations.index(("reset", 2)) < first_generation_two_append
    assert generator.calls[1]["previous_error"] is not None
    assert all(stream.closed for stream in generator.streams)
    assert (generator.scope_enters, generator.scope_exits) == (1, 1)
    assert generator.events.index("stream_close:1") < generator.events.index("invoke:2")
    assert generator.events[-2:] == ["stream_close:2", "scope_exit"]


@pytest.mark.asyncio
async def test_two_retryable_failures_reset_to_generation_three_fallback(
    capfire: CaptureLogfire,
) -> None:
    generator = FakeGenerator(["引用がありません。", "まだ引用がありません。"])
    reporter = RecordingDeltaReporter()

    outcome = await _answer(generator, delta_reporter=reporter)

    assert getattr(outcome, "failure_code", None) == "answer_synthesis_draft_invalid"
    assert reporter.aborted == [1, 2]
    assert reporter.reset_generations == [2, 3]
    assert reporter.finished == [3]
    assert all(generation != 3 for generation, _ in reporter.appended)
    operations = _operation_names(reporter)
    assert operations.index(("abort", 1)) < operations.index(("reset", 2))
    assert operations.index(("abort", 2)) < operations.index(("reset", 3))
    metrics = collected_metrics(capfire)
    assert sum_counter_for_result(metrics, _SYNTHESIS_OUTCOME_METRIC, "fallback") == 1


@pytest.mark.asyncio
async def test_provider_error_resets_once_then_falls_back_without_live_delta() -> None:
    """生成不能の定型本文はflow側からlive deltaに流れない(定型化はresult_assembly
    の責務)。resetとcloseは現行どおり発火する。
    """
    generator = FakeGenerator([AIProviderNetworkError()])
    reporter = RecordingDeltaReporter()

    outcome = await _answer(generator, delta_reporter=reporter)

    assert getattr(outcome, "failure_code", None) == "ai_error_network"
    assert reporter.aborted == [1]
    assert reporter.reset_generations == [2]
    assert reporter.finished == [2]
    assert all(generation != 2 for generation, _ in reporter.appended)
    assert generator.streams[0].closed is True


@pytest.mark.asyncio
async def test_all_reporter_failures_do_not_change_retry_result() -> None:
    generator = FakeGenerator(["引用がありません。", "修正後の回答です。[[1]]"])
    reporter = RecordingDeltaReporter(
        fail_on=frozenset({"append", "finish", "abort", "reset"})
    )

    draft = await _answer(generator, delta_reporter=reporter)

    assert draft == _expected_draft(
        answer="修正後の回答です。[[1]]",
        cited_refs=["1"],
    )
    assert reporter.aborted == [1]
    assert reporter.reset_generations == [2]
    assert reporter.finished == [2]
    assert any(generation == 2 for generation, _ in reporter.appended)


@pytest.mark.parametrize("with_failing_reporter", [False, True])
@pytest.mark.asyncio
async def test_reporter_is_not_part_of_final_draft_correctness(
    with_failing_reporter: bool,
) -> None:
    generator = FakeGenerator(["根拠から確認できます。[[1]]"])
    reporter = (
        RecordingDeltaReporter(fail_on=frozenset({"append", "finish"}))
        if with_failing_reporter
        else None
    )

    draft = await _answer(generator, delta_reporter=reporter)

    assert draft == _expected_draft(
        answer="根拠から確認できます。[[1]]",
        cited_refs=["1"],
    )


@pytest.mark.asyncio
async def test_continuation_false_before_provider_start_is_routine_stop(
    capfire: CaptureLogfire,
) -> None:
    stopped_type = _answer_generation_stopped_type()
    assert not issubclass(stopped_type, AIProviderError)
    generator = FakeGenerator(["根拠から確認できます。[[1]]"])
    reporter = RecordingDeltaReporter()

    with pytest.raises(stopped_type):
        await _answer(
            generator,
            delta_reporter=reporter,
            continuation=SequenceContinuation([False]),
        )

    assert generator.calls == []
    assert generator.streams == []
    assert reporter.aborted == [1]
    assert reporter.appended == []
    assert reporter.finished == []
    metrics = collected_metrics(capfire)
    assert _metric_attributes(metrics, _SYNTHESIS_OUTCOME_METRIC) == []
    phase = one_span_named(capfire, _PHASE_SPAN_NAME)
    assert exception_event(phase) is None
    assert phase.get("status", {}).get("description") in (None, "")
    assert (generator.scope_enters, generator.scope_exits) == (1, 1)


@pytest.mark.asyncio
async def test_continuation_false_mid_stream_closes_and_aborts(
    capfire: CaptureLogfire,
) -> None:
    stopped_type = _answer_generation_stopped_type()
    generator = FakeGenerator([["表示済み本文と", "非表示本文。[[1]]"]])
    reporter = RecordingDeltaReporter()
    continuation = SequenceContinuation([True, True, False])

    with pytest.raises(stopped_type):
        await _answer(
            generator,
            delta_reporter=reporter,
            continuation=continuation,
        )

    assert "".join(text for _, text in reporter.appended) == "表示済み本文と"
    assert reporter.aborted == [1]
    assert reporter.finished == []
    assert generator.streams[0].closed is True
    metrics = collected_metrics(capfire)
    assert _metric_attributes(metrics, _SYNTHESIS_OUTCOME_METRIC) == []


@pytest.mark.asyncio
async def test_continuation_false_at_eof_stops_before_final_parse_and_metric(
    capfire: CaptureLogfire,
) -> None:
    stopped_type = _answer_generation_stopped_type()
    generator = FakeGenerator(["根拠から確認できます。[[1]]"])
    reporter = RecordingDeltaReporter()
    continuation = SequenceContinuation([True, True, False])

    with pytest.raises(stopped_type):
        await _answer(
            generator,
            delta_reporter=reporter,
            continuation=continuation,
        )

    assert reporter.aborted == [1]
    assert reporter.finished == []
    assert reporter.reset_generations == []
    assert generator.streams[0].closed is True
    metrics = collected_metrics(capfire)
    assert _metric_attributes(metrics, _SYNTHESIS_OUTCOME_METRIC) == []


@pytest.mark.asyncio
async def test_continuation_false_after_provider_error_stops_before_fallback(
    capfire: CaptureLogfire,
) -> None:
    stopped_type = _answer_generation_stopped_type()
    generator = FakeGenerator([AIProviderNetworkError()])
    reporter = RecordingDeltaReporter()

    with pytest.raises(stopped_type):
        await _answer(
            generator,
            delta_reporter=reporter,
            continuation=SequenceContinuation([True, False]),
        )

    assert generator.streams[0].closed is True
    assert reporter.aborted == [1]
    assert reporter.reset_generations == []
    assert reporter.appended == []
    assert reporter.finished == []
    metrics = collected_metrics(capfire)
    assert _metric_attributes(metrics, _SYNTHESIS_OUTCOME_METRIC) == []


@pytest.mark.asyncio
async def test_truncated_first_attempt_retries_with_trusted_truncation_notice(
    capfire: CaptureLogfire,
) -> None:
    """MAX_TOKENS打ち切りはrequest内でretryされ、通知はtrusted側の入力で運ばれる。"""
    generator = FakeGenerator([_truncated_error(), "根拠から確認できます。[[1]]"])

    draft = await _answer(generator)

    assert len(generator.calls) == 2
    assert getattr(generator.inputs[0], "previous_output_truncated", None) is False
    assert getattr(generator.inputs[1], "previous_output_truncated", None) is True
    assert draft == _expected_draft(
        answer="根拠から確認できます。[[1]]",
        cited_refs=["1"],
    )
    metrics = collected_metrics(capfire)
    assert _metric_attributes(metrics, _SYNTHESIS_OUTCOME_METRIC) == [
        {
            "result": "synthesized",
            "retry_used": True,
            "fallback_used": False,
            "failure_code": "none",
        }
    ]


@pytest.mark.asyncio
async def test_non_truncation_retry_does_not_carry_truncation_notice() -> None:
    """打ち切り以外が原因のretryでは打ち切り通知フラグを立てない。"""
    generator = FakeGenerator(["引用がありません。", "根拠から確認できます。[[1]]"])

    draft = await _answer(generator)

    assert len(generator.calls) == 2
    assert getattr(generator.inputs[1], "previous_output_truncated", None) is False
    assert draft == _expected_draft(
        answer="根拠から確認できます。[[1]]",
        cited_refs=["1"],
    )


@pytest.mark.asyncio
async def test_second_truncation_falls_back_with_truncated_failure_code(
    capfire: CaptureLogfire,
) -> None:
    """2回連続のMAX_TOKENSはattempt数2を使い切りfallbackへ落ちる。"""
    generator = FakeGenerator([_truncated_error(), _truncated_error()])

    await _answer(generator)

    assert len(generator.calls) == 2
    metrics = collected_metrics(capfire)
    attrs = _metric_attributes(metrics, _SYNTHESIS_OUTCOME_METRIC)
    assert len(attrs) == 1
    assert attrs[0]["retry_used"] is True
    assert attrs[0]["result"] == "fallback"
    assert attrs[0]["failure_code"] == "ai_error_output_truncated"


@pytest.mark.asyncio
async def test_truncation_mid_stream_aborts_live_draft_and_resets_generation_two() -> (
    None
):
    """途中までfragmentが流れていても打ち切りはlive draftをabortしてresetする。"""
    generator = FakeGenerator(
        [
            ["打ち切り前に表示された本文。", _truncated_error()],
            "打ち切り後に修正した回答です。[[1]]",
        ]
    )
    reporter = RecordingDeltaReporter()

    draft = await _answer(generator, delta_reporter=reporter)

    assert len(generator.calls) == 2
    assert draft == _expected_draft(
        answer="打ち切り後に修正した回答です。[[1]]",
        cited_refs=["1"],
    )
    visible_before_abort = "".join(
        text for generation, text in reporter.appended if generation == 1
    )
    assert visible_before_abort
    assert reporter.aborted == [1]
    assert reporter.reset_generations == [2]
    assert reporter.finished == [2]
    operations = _operation_names(reporter)
    assert operations.index(("abort", 1)) < operations.index(("reset", 2))
    assert all(stream.closed for stream in generator.streams)
