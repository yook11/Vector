"""Agent run live updateのfan-out reporter契約テスト。"""

from __future__ import annotations

import json
from typing import Literal, cast
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from structlog.testing import capture_logs

from app.agent.contract import (
    AnswerProgressEvent,
    AnswerProgressStage,
    ExternalSearchCandidatesFetchedEvent,
    ExternalSearchEvidenceSelectedEvent,
    ExternalSearchQueriesGeneratedEvent,
    InternalSearchCompletedEvent,
    InternalSearchStartedEvent,
    QuestionResolvedEvent,
)
from app.agent.live_updates import reporters
from app.agent.live_updates.stream import (
    AgentRunLiveStreamActivityEvent,
    AgentRunLiveStreamPublisher,
    AgentRunLiveStreamStageEvent,
)

RUN_ID = UUID("00000000-0000-4000-a000-000000000012")
ATTEMPT_EPOCH = 7
SECRET_PAYLOAD = "SECRET_QUESTION_AND_QUERY"
SECRET_EXCEPTION = "SECRET_SINK_EXCEPTION"


class RecordingPipeline:
    def __init__(self) -> None:
        self.entries: list[dict[str, str]] = []

    def xadd(
        self,
        _key: str,
        fields: dict[str, str],
        *,
        maxlen: int,
        approximate: bool,
    ) -> RecordingPipeline:
        assert maxlen > 0
        assert approximate is False
        self.entries.append(fields)
        return self

    def expire(self, _key: str, _seconds: int) -> RecordingPipeline:
        return self

    async def execute(self) -> list[object]:
        return [
            *(f"{index}-0" for index in range(1, len(self.entries) + 1)),
            True,
        ]


class RecordingRedis:
    def __init__(self) -> None:
        self.pipeline_instance = RecordingPipeline()

    def pipeline(self) -> RecordingPipeline:
        return self.pipeline_instance


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["planning", "retrieving", "synthesizing"])
async def test_stage_reporter_fans_out_each_stage_without_mutation(
    stage: Literal["planning", "retrieving", "synthesizing"],
) -> None:
    progress_writer = AsyncMock()
    stream_publisher = AsyncMock()
    stream_publisher.publish.return_value = None
    reporter = reporters.AgentRunLiveStageReporter(progress_writer, stream_publisher)

    await reporter.stage_changed(stage)

    progress_writer.stage_changed.assert_awaited_once_with(stage)
    stream_publisher.publish.assert_awaited_once_with(
        AgentRunLiveStreamStageEvent(stage=stage)
    )


@pytest.mark.asyncio
async def test_stage_reporter_attempts_stream_when_progress_writer_raises() -> None:
    progress_writer = AsyncMock()
    progress_writer.stage_changed.side_effect = RuntimeError("database unavailable")
    stream_publisher = AsyncMock()
    reporter = reporters.AgentRunLiveStageReporter(progress_writer, stream_publisher)

    await reporter.stage_changed("planning")

    stream_publisher.publish.assert_awaited_once_with(
        AgentRunLiveStreamStageEvent(stage="planning")
    )


@pytest.mark.asyncio
async def test_stage_reporter_attempts_progress_writer_when_stream_raises() -> None:
    progress_writer = AsyncMock()
    stream_publisher = AsyncMock()
    stream_publisher.publish.side_effect = RuntimeError("stream unavailable")
    reporter = reporters.AgentRunLiveStageReporter(progress_writer, stream_publisher)

    await reporter.stage_changed("retrieving")

    progress_writer.stage_changed.assert_awaited_once_with("retrieving")


@pytest.mark.asyncio
async def test_stage_projection_failure_still_attempts_progress_writer() -> None:
    progress_writer = AsyncMock()
    stream_publisher = AsyncMock()
    reporter = reporters.AgentRunLiveStageReporter(progress_writer, stream_publisher)
    invalid_stage = cast(AnswerProgressStage, "invalid")

    await reporter.stage_changed(invalid_stage)

    progress_writer.stage_changed.assert_awaited_once_with(invalid_stage)
    stream_publisher.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_stage_reporter_uses_stream_publishers_attempt_epoch() -> None:
    redis = RecordingRedis()
    stream_publisher = AgentRunLiveStreamPublisher(
        redis,  # type: ignore[arg-type]
        RUN_ID,
        ATTEMPT_EPOCH,
    )
    reporter = reporters.AgentRunLiveStageReporter(AsyncMock(), stream_publisher)

    await reporter.stage_changed("synthesizing")

    assert [entry["attemptEpoch"] for entry in redis.pipeline_instance.entries] == [
        str(ATTEMPT_EPOCH),
        str(ATTEMPT_EPOCH),
    ]


KNOWN_ACTIVITIES = [
    InternalSearchStartedEvent(query_count=2),
    InternalSearchCompletedEvent(hit_count=3),
    ExternalSearchQueriesGeneratedEvent(
        task_index=1,
        queries=["semiconductor outlook"],
    ),
    ExternalSearchCandidatesFetchedEvent(task_index=1, candidate_count=4),
    ExternalSearchEvidenceSelectedEvent(task_index=1, evidence_count=2),
    QuestionResolvedEvent(standalone_question="What changed?"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "activity",
    KNOWN_ACTIVITIES,
    ids=lambda activity: activity.type,
)
async def test_activity_reporter_fans_out_each_known_event(
    activity: AnswerProgressEvent,
) -> None:
    list_publisher = AsyncMock()
    stream_publisher = AsyncMock()
    stream_publisher.publish.return_value = None
    reporter = reporters.AgentRunLiveActivityReporter(list_publisher, stream_publisher)

    await reporter.event_occurred(activity)

    list_publisher.event_occurred.assert_awaited_once_with(activity)
    stream_publisher.publish.assert_awaited_once_with(
        AgentRunLiveStreamActivityEvent(activity=activity)
    )


@pytest.mark.asyncio
async def test_activity_reporter_attempts_stream_when_list_publisher_raises() -> None:
    activity = InternalSearchStartedEvent(query_count=2)
    list_publisher = AsyncMock()
    list_publisher.event_occurred.side_effect = RuntimeError("list unavailable")
    stream_publisher = AsyncMock()
    reporter = reporters.AgentRunLiveActivityReporter(list_publisher, stream_publisher)

    await reporter.event_occurred(activity)

    stream_publisher.publish.assert_awaited_once_with(
        AgentRunLiveStreamActivityEvent(activity=activity)
    )


@pytest.mark.asyncio
async def test_activity_reporter_attempts_list_when_stream_publisher_raises() -> None:
    activity = InternalSearchCompletedEvent(hit_count=3)
    list_publisher = AsyncMock()
    stream_publisher = AsyncMock()
    stream_publisher.publish.side_effect = RuntimeError("stream unavailable")
    reporter = reporters.AgentRunLiveActivityReporter(list_publisher, stream_publisher)

    await reporter.event_occurred(activity)

    list_publisher.event_occurred.assert_awaited_once_with(activity)


@pytest.mark.asyncio
async def test_activity_projection_failure_still_attempts_list_publisher() -> None:
    invalid_activity = cast(AnswerProgressEvent, object())
    list_publisher = AsyncMock()
    stream_publisher = AsyncMock()
    reporter = reporters.AgentRunLiveActivityReporter(
        list_publisher,
        stream_publisher,
    )

    await reporter.event_occurred(invalid_activity)

    list_publisher.event_occurred.assert_awaited_once_with(invalid_activity)
    stream_publisher.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_activity_reporter_preserves_nested_domain_shape() -> None:
    activity = ExternalSearchCandidatesFetchedEvent(
        task_index=2,
        candidate_count=5,
    )
    stream_publisher = AsyncMock()
    reporter = reporters.AgentRunLiveActivityReporter(AsyncMock(), stream_publisher)

    await reporter.event_occurred(activity)

    published = stream_publisher.publish.await_args.args[0]
    assert published.model_dump(mode="json") == {
        "type": "activity",
        "activity": {
            "type": "external_search.candidates_fetched",
            "task_index": 2,
            "candidate_count": 5,
        },
    }


@pytest.mark.asyncio
async def test_activity_reporter_does_not_log_payload_or_exception_text() -> None:
    activity = QuestionResolvedEvent(standalone_question=SECRET_PAYLOAD)
    list_publisher = AsyncMock()
    list_publisher.event_occurred.side_effect = RuntimeError(SECRET_EXCEPTION)
    stream_publisher = AsyncMock()
    stream_publisher.publish.side_effect = RuntimeError(SECRET_EXCEPTION)
    reporter = reporters.AgentRunLiveActivityReporter(list_publisher, stream_publisher)

    with capture_logs() as logs:
        await reporter.event_occurred(activity)

    encoded_logs = json.dumps(logs, ensure_ascii=False, default=str)
    assert SECRET_PAYLOAD not in encoded_logs
    assert SECRET_EXCEPTION not in encoded_logs
