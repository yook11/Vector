"""Answer delta producerから実Redis readerまでの境界検証。"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import redis.asyncio as aioredis
from pydantic import ValidationError

from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.direct_answer.agent import DIRECT_ANSWER_AGENT
from app.agent.answering.direct_answer.contract import DirectAnswerDraft
from app.agent.answering.direct_answer.flow import DirectAnswerFlow
from app.agent.answering.evidence_answer.agent import EVIDENCE_ANSWER_AGENT
from app.agent.answering.evidence_answer.contract import EvidenceAnswerDraft
from app.agent.answering.evidence_answer.evidence import AnswerEvidenceItem
from app.agent.answering.evidence_answer.flow import EvidenceAnswerFlow
from app.agent.contract import (
    ExternalSearchCandidatesFetchedEvent,
    ExternalUrlSource,
)
from app.agent.live_updates.answer_delta import AgentRunLiveAnswerDeltaReporter
from app.agent.live_updates.recent_events import (
    AgentRunLiveEventPublisher,
    AgentRunLiveEventReader,
    agent_run_live_events_key,
)
from app.agent.live_updates.stream import (
    AgentRunLiveStreamActivityEvent,
    AgentRunLiveStreamAnswerDeltaEvent,
    AgentRunLiveStreamAnswerResetEvent,
    AgentRunLiveStreamAttemptStartedEvent,
    AgentRunLiveStreamPublisher,
    AgentRunLiveStreamReader,
    AgentRunLiveStreamReadStatus,
    AgentRunLiveStreamStageEvent,
    AgentRunLiveStreamTerminalEvent,
    agent_run_live_stream_key,
    is_stream_id_before,
)
from app.agent.planning.contract import TargetTimeWindow
from app.agent.question_context.contract import AnswerRequirement, QuestionContext
from app.config import settings

pytestmark = pytest.mark.xdist_group("redis")


class FakeStreamingGenerator:
    def __init__(self, generations: Sequence[Sequence[str]]) -> None:
        self._generations = deque([list(chunks) for chunks in generations])

    def invoke_stream(
        self,
        _agent: object,
        _input: object,
        *,
        attempt_number: int,  # noqa: ARG002
    ) -> AsyncIterator[str]:
        chunks = self._generations.popleft()

        async def generate() -> AsyncIterator[str]:
            for chunk in chunks:
                yield chunk

        return generate()

    @asynccontextmanager
    async def activate(self) -> AsyncIterator[FakeStreamingGenerator]:
        yield self


def _answering_request(
    question: str,
    *,
    content_requirements: list[AnswerRequirement] | None = None,
    response_requirements: list[AnswerRequirement] | None = None,
) -> AnsweringRequest:
    return AnsweringRequest(
        context=QuestionContext(
            standalone_question=question,
            content_requirements=content_requirements or [],
            response_requirements=response_requirements or [],
        ),
        as_of=datetime(2026, 7, 12, tzinfo=UTC),
    )


class FailingDeltaPublisher:
    def __init__(self, delegate: AgentRunLiveStreamPublisher) -> None:
        self.delegate = delegate
        self.calls: list[AgentRunLiveStreamAnswerDeltaEvent] = []

    async def publish(
        self,
        event: AgentRunLiveStreamAnswerDeltaEvent,
    ) -> str | None:
        self.calls.append(event)
        return None


class ResetDroppingPublisher:
    def __init__(self, delegate: AgentRunLiveStreamPublisher) -> None:
        self.delegate = delegate
        self.dropped_resets: list[AgentRunLiveStreamAnswerResetEvent] = []
        self.delegated_deltas: list[AgentRunLiveStreamAnswerDeltaEvent] = []

    async def publish(
        self,
        event: AgentRunLiveStreamAnswerDeltaEvent | AgentRunLiveStreamAnswerResetEvent,
    ) -> str | None:
        if isinstance(event, AgentRunLiveStreamAnswerResetEvent):
            self.dropped_resets.append(event)
            return None
        self.delegated_deltas.append(event)
        return await self.delegate.publish(event)


async def _answer(
    generator: FakeStreamingGenerator,
    reporter: AgentRunLiveAnswerDeltaReporter,
) -> DirectAnswerDraft:
    return await DirectAnswerFlow(
        agent=DIRECT_ANSWER_AGENT,
        runtime_scope_factory=generator.activate,
        delta_reporter=reporter,
    ).answer(
        request=_answering_request("実Redisへのdelta配信を確認する"),
    )


async def _evidence_answer(
    generator: FakeStreamingGenerator,
    reporter: AgentRunLiveAnswerDeltaReporter,
    *,
    request: AnsweringRequest | None = None,
) -> EvidenceAnswerDraft:
    try:
        return await EvidenceAnswerFlow(
            agent=EVIDENCE_ANSWER_AGENT,
            runtime_scope_factory=generator.activate,
            delta_reporter=reporter,
        ).answer(
            request=(
                _answering_request("実RedisへのEvidence revision配信を確認する")
                if request is None
                else request
            ),
            evidence=[
                AnswerEvidenceItem(
                    source=ExternalUrlSource(
                        source_ref="1",
                        url="https://example.com/evidence-1",
                        title="Evidence source",
                        evidence_claim="根拠を確認しました。",
                    ),
                    text="根拠を確認しました。",
                )
            ],
            target_time_window=TargetTimeWindow(kind="today"),
            review_missing=(),
        )
    except TypeError:
        pytest.fail(
            "S5追補: EvidenceAnswerFlow.answer must accept a required "
            "review_missing keyword argument (tuple[str, ...])"
        )


def _delta_events(
    events: Sequence[object],
) -> list[AgentRunLiveStreamAnswerDeltaEvent]:
    return [
        event
        for event in events
        if isinstance(event, AgentRunLiveStreamAnswerDeltaEvent)
    ]


def _expected_evidence_draft(*, answer: str, cited_refs: list[str]) -> object:
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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_evidence_json_shaped_fragment_flows_unchanged_into_answer_delta() -> (
    None
):
    """S4: response_schema=Noneのplain text streamでは、JSONを模した本文が

    field抽出されずそのまま本文になる(条件2・3)。live表示はmarkerだけを除く。
    """
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    run_id = uuid4()
    attempt_epoch = 13
    stream_key = agent_run_live_stream_key(run_id)
    final_answer = '{"answer":"本文","cited_refs":["1"]}確認できます。[[1]]'
    visible_answer = '{"answer":"本文","cited_refs":["1"]}確認できます。'
    fragments = [
        final_answer[index : index + 7] for index in range(0, len(final_answer), 7)
    ]
    request = _answering_request(
        "JSON形状の本文がそのまま扱われるか確認する",
        content_requirements=[
            AnswerRequirement(requirement_id="c1", description="content requirement")
        ],
        response_requirements=[
            AnswerRequirement(requirement_id="p1", description="response requirement")
        ],
    )
    try:
        stream_publisher = AgentRunLiveStreamPublisher(redis, run_id, attempt_epoch)
        assert await stream_publisher.begin_attempt() is not None
        reporter = AgentRunLiveAnswerDeltaReporter(
            stream_publisher,
            run_id=run_id,
            attempt_epoch=attempt_epoch,
        )

        draft = await _evidence_answer(
            FakeStreamingGenerator([fragments]),
            reporter,
            request=request,
        )
        result = await AgentRunLiveStreamReader(redis).read_after(
            run_id,
            attempt_epoch,
            None,
        )

        deltas = _delta_events([entry.event for entry in result.events])
        visible = "".join(event.text for event in deltas)
        assert draft == _expected_evidence_draft(answer=final_answer, cited_refs=["1"])
        assert (visible, {event.generation for event in deltas}) == (
            visible_answer,
            {1},
        )
    finally:
        await redis.delete(stream_key)
        await redis.aclose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_direct_flow_round_trip_matches_final_answer_and_envelope() -> None:
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    run_id = uuid4()
    attempt_epoch = 3
    stream_key = agent_run_live_stream_key(run_id)
    try:
        stream_publisher = AgentRunLiveStreamPublisher(
            redis,
            run_id,
            attempt_epoch,
        )
        marker_id = await stream_publisher.begin_attempt()
        assert marker_id is not None
        reporter = AgentRunLiveAnswerDeltaReporter(
            stream_publisher,
            run_id=run_id,
            attempt_epoch=attempt_epoch,
        )
        draft = await _answer(
            FakeStreamingGenerator([[" \n本文 [[", "1]] 続き\t "]]),
            reporter,
        )

        result = await AgentRunLiveStreamReader(redis).read_after(
            run_id,
            attempt_epoch,
            None,
        )

        assert draft == DirectAnswerDraft(answer="本文  続き")
        assert result.status is AgentRunLiveStreamReadStatus.EVENTS
        assert isinstance(result.events[0].event, AgentRunLiveStreamAttemptStartedEvent)
        deltas = _delta_events([entry.event for entry in result.events])
        assert "".join(event.text for event in deltas) == draft.answer
        assert {event.generation for event in deltas} == {1}
        assert all(entry.attempt_epoch == attempt_epoch for entry in result.events)
        assert result.events[0].stream_id == marker_id
        assert all(
            is_stream_id_before(left.stream_id, right.stream_id)
            for left, right in zip(result.events, result.events[1:])
        )

        raw_entries = await redis.xrange(stream_key)
        assert [fields["type"] for _id, fields in raw_entries] == [
            "attempt.started",
            "answer.delta",
        ]
        assert set(raw_entries[1][1]) == {
            "type",
            "attemptEpoch",
            "payload",
            "publishedAt",
        }
        assert raw_entries[1][1]["attemptEpoch"] == str(attempt_epoch)
        assert json.loads(raw_entries[1][1]["payload"]) == {
            "generation": 1,
            "text": draft.answer,
        }
    finally:
        await redis.delete(stream_key)
        await redis.aclose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_blank_generation_retries_without_reset_and_splits_generation_two() -> (
    None
):
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    run_id = uuid4()
    attempt_epoch = 5
    stream_key = agent_run_live_stream_key(run_id)
    answer = "界" * 1025
    try:
        stream_publisher = AgentRunLiveStreamPublisher(
            redis,
            run_id,
            attempt_epoch,
        )
        assert await stream_publisher.begin_attempt() is not None
        reporter = AgentRunLiveAnswerDeltaReporter(
            stream_publisher,
            run_id=run_id,
            attempt_epoch=attempt_epoch,
        )

        draft = await _answer(
            FakeStreamingGenerator(
                [
                    [" [[", "1]] \n"],
                    [answer[:400], answer[400:900], answer[900:]],
                ]
            ),
            reporter,
        )
        result = await AgentRunLiveStreamReader(redis).read_after(
            run_id,
            attempt_epoch,
            None,
        )

        assert draft == DirectAnswerDraft(answer=answer)
        assert result.status is AgentRunLiveStreamReadStatus.EVENTS
        events = [entry.event for entry in result.events]
        deltas = _delta_events(events)
        assert [len(event.text) for event in deltas] == [512, 512, 1]
        assert {event.generation for event in deltas} == {2}
        assert "".join(event.text for event in deltas) == draft.answer
        assert not any(event.type == "answer.reset" for event in events)
        assert not any(
            isinstance(event, AgentRunLiveStreamAnswerDeltaEvent)
            and event.generation == 1
            for event in events
        )
    finally:
        await redis.delete(stream_key)
        await redis.aclose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_evidence_retry_round_trip_preserves_reset_delta_and_envelope() -> None:
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    run_id = uuid4()
    attempt_epoch = 11
    stream_key = agent_run_live_stream_key(run_id)
    visible_answer = "根拠を確認  しました。"
    try:
        stream_publisher = AgentRunLiveStreamPublisher(
            redis,
            run_id,
            attempt_epoch,
        )
        marker_id = await stream_publisher.begin_attempt()
        assert marker_id is not None
        reporter = AgentRunLiveAnswerDeltaReporter(
            stream_publisher,
            run_id=run_id,
            attempt_epoch=attempt_epoch,
        )
        generator = FakeStreamingGenerator(
            [
                # evidenceに存在しないref([[9]])はunknown citation refとして
                # 不正になりretryされる。marker全体がAnswerVisibleTextFilterに
                # 吸収されるため、generation 1にvisible delta自体が発生しない。
                ["[[9]]"],
                [
                    "根拠を確認 [[",
                    "1]] しました。",
                ],
            ]
        )

        draft = await _evidence_answer(generator, reporter)
        result = await AgentRunLiveStreamReader(redis).read_after(
            run_id,
            attempt_epoch,
            None,
        )

        assert draft == _expected_evidence_draft(
            answer="根拠を確認 [[1]] しました。",
            cited_refs=["1"],
        )
        assert result.status is AgentRunLiveStreamReadStatus.EVENTS
        assert [entry.event for entry in result.events] == [
            AgentRunLiveStreamAttemptStartedEvent(),
            AgentRunLiveStreamAnswerResetEvent(generation=2),
            AgentRunLiveStreamAnswerDeltaEvent(
                generation=2,
                text=visible_answer,
            ),
        ]
        assert all(entry.attempt_epoch == attempt_epoch for entry in result.events)
        assert result.events[0].stream_id == marker_id
        assert all(
            is_stream_id_before(left.stream_id, right.stream_id)
            for left, right in zip(result.events, result.events[1:])
        )

        raw_entries = await redis.xrange(stream_key)
        assert [fields["type"] for _id, fields in raw_entries] == [
            "attempt.started",
            "answer.reset",
            "answer.delta",
        ]
        assert all(
            set(fields) == {"type", "attemptEpoch", "payload", "publishedAt"}
            for _id, fields in raw_entries
        )
        assert {fields["attemptEpoch"] for _id, fields in raw_entries} == {
            str(attempt_epoch)
        }
        assert [json.loads(fields["payload"]) for _id, fields in raw_entries] == [
            {},
            {"generation": 2},
            {"generation": 2, "text": visible_answer},
        ]
        raw_ids = [stream_id for stream_id, _fields in raw_entries]
        assert all(
            is_stream_id_before(left, right)
            for left, right in zip(raw_ids, raw_ids[1:])
        )
    finally:
        await redis.delete(stream_key)
        await redis.aclose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_evidence_higher_generation_delta_survives_reset_loss() -> None:
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    run_id = uuid4()
    attempt_epoch = 12
    stream_key = agent_run_live_stream_key(run_id)
    visible_answer = "resetなしでも修正版を表示します。"
    try:
        stream_publisher = AgentRunLiveStreamPublisher(
            redis,
            run_id,
            attempt_epoch,
        )
        assert await stream_publisher.begin_attempt() is not None
        reset_dropping_publisher = ResetDroppingPublisher(stream_publisher)
        reporter = AgentRunLiveAnswerDeltaReporter(
            reset_dropping_publisher,
            run_id=run_id,
            attempt_epoch=attempt_epoch,
        )
        generator = FakeStreamingGenerator(
            [
                # evidenceに存在しないref([[9]])はunknown citation refとして
                # 不正になりretryされる。marker全体がAnswerVisibleTextFilterに
                # 吸収されるため、generation 1にvisible delta自体が発生しない。
                ["[[9]]"],
                [
                    "resetなしでも修正版を表示します。[[",
                    "1]]",
                ],
            ]
        )

        draft = await _evidence_answer(generator, reporter)
        result = await AgentRunLiveStreamReader(redis).read_after(
            run_id,
            attempt_epoch,
            None,
        )

        assert draft == _expected_evidence_draft(
            answer="resetなしでも修正版を表示します。[[1]]",
            cited_refs=["1"],
        )
        assert reset_dropping_publisher.dropped_resets == [
            AgentRunLiveStreamAnswerResetEvent(generation=2)
        ]
        assert reset_dropping_publisher.delegated_deltas == [
            AgentRunLiveStreamAnswerDeltaEvent(
                generation=2,
                text=visible_answer,
            )
        ]
        assert result.status is AgentRunLiveStreamReadStatus.EVENTS
        assert [entry.event for entry in result.events] == [
            AgentRunLiveStreamAttemptStartedEvent(),
            AgentRunLiveStreamAnswerDeltaEvent(
                generation=2,
                text=visible_answer,
            ),
        ]
        assert not any(
            isinstance(entry.event, AgentRunLiveStreamAnswerResetEvent)
            for entry in result.events
        )
        assert all(entry.attempt_epoch == attempt_epoch for entry in result.events)
        assert is_stream_id_before(
            result.events[0].stream_id,
            result.events[1].stream_id,
        )
    finally:
        await redis.delete(stream_key)
        await redis.aclose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delta_breaker_does_not_damage_other_stream_or_list_producers() -> None:
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    run_id = uuid4()
    attempt_epoch = 8
    stream_key = agent_run_live_stream_key(run_id)
    list_key = agent_run_live_events_key(run_id)
    try:
        stream_publisher = AgentRunLiveStreamPublisher(
            redis,
            run_id,
            attempt_epoch,
        )
        assert await stream_publisher.begin_attempt() is not None
        failing_delta_publisher = FailingDeltaPublisher(stream_publisher)
        reporter = AgentRunLiveAnswerDeltaReporter(
            failing_delta_publisher,
            run_id=run_id,
            attempt_epoch=attempt_epoch,
        )

        for marker in "ABCD":
            await reporter.append(generation=1, text=marker * 512)
        await reporter.finish(generation=1)

        activity = ExternalSearchCandidatesFetchedEvent(
            task_index=2,
            candidate_count=5,
        )
        await stream_publisher.publish(
            AgentRunLiveStreamStageEvent(stage="evidence_collection")
        )
        await stream_publisher.publish(
            AgentRunLiveStreamActivityEvent(activity=activity)
        )
        await AgentRunLiveEventPublisher(redis, run_id).event_occurred(activity)
        await stream_publisher.publish(
            AgentRunLiveStreamTerminalEvent(status="completed")
        )

        stream_result = await AgentRunLiveStreamReader(redis).read_after(
            run_id,
            attempt_epoch,
            None,
        )
        recent_events = await AgentRunLiveEventReader(redis).recent_events(run_id)

        assert len(failing_delta_publisher.calls) == 3
        assert stream_result.status is AgentRunLiveStreamReadStatus.EVENTS
        stream_events = [entry.event for entry in stream_result.events]
        assert [event.type for event in stream_events] == [
            "attempt.started",
            "stage",
            "activity",
            "terminal",
        ]
        assert _delta_events(stream_events) == []
        assert isinstance(stream_events[1], AgentRunLiveStreamStageEvent)
        assert stream_events[1].stage == "evidence_collection"
        assert isinstance(stream_events[2], AgentRunLiveStreamActivityEvent)
        assert stream_events[2].activity == activity
        assert stream_events[3] == AgentRunLiveStreamTerminalEvent(status="completed")
        assert len(recent_events) == 1
        assert (
            recent_events[0].type
            == "evidence_collection.external_search_candidates_fetched"
        )
        assert recent_events[0].task_index == 2
        assert recent_events[0].candidate_count == 5
    finally:
        await redis.delete(stream_key, list_key)
        await redis.aclose()
