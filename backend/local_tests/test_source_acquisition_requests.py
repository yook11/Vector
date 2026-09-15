"""実DBの対象選定からSQS送信・再送・最終失敗までを検証する。"""

import json
from collections import deque
from hashlib import md5

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from structlog.testing import capture_logs

from app.collection.article_acquisition.acquisition_dispatch import (
    AcquisitionDispatchError,
    SourceAcquisitionDispatcher,
)
from app.collection.article_acquisition.sqs_acquisition_sender import (
    open_acquisition_sender,
)
from app.collection.sources.acquisition_request import SourceAcquisitionSchedule
from app.collection.sources.dispatch import SourceDispatchService

QUEUE_URL = "https://sqs.ap-northeast-1.amazonaws.com/123456789012/source-acquisitions"
SCHEDULE = {"cadence": "high", "scheduled_at": "2026-09-13T10:00:00+09:00"}


def service_error(code, status=400):
    return ClientError(
        {
            "Error": {"Code": code, "Message": "private-sdk-message"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "SendMessage",
    )


class ScriptedSqsClient:
    """SDK境界でのみ応答を制御し、実際に渡された本文を記録する。"""

    def __init__(self, actions, calls):
        self.actions = actions
        self.calls = calls

    def send_message(self, *, QueueUrl, MessageBody):
        self.calls.append((QueueUrl, MessageBody))
        source_id = json.loads(MessageBody)["source_id"]
        action = self.actions[source_id].popleft()
        if isinstance(action, Exception):
            raise action
        if action is not None:
            return action
        return {
            "MessageId": "sqs-message-id",
            "MD5OfMessageBody": md5(
                MessageBody.encode(), usedforsecurity=False
            ).hexdigest(),
        }

    def close(self):
        pass


@pytest.fixture
async def acquisition_sources(system_database):
    async with system_database.connect("vector") as db:
        await db.execute("UPDATE news_sources SET is_active = false")
        rows = await db.fetch(
            "UPDATE news_sources SET is_active = true "
            "WHERE name IN ('Engadget', 'TechCrunch', 'OpenAI') RETURNING id, name"
        )
        ids = {row["name"]: row["id"] for row in rows}
        assert set(ids) == {"Engadget", "TechCrunch", "OpenAI"}
        unknown_id = await db.fetchval(
            "INSERT INTO news_sources "
            "(name, source_type, site_url, endpoint_url, is_active) "
            "VALUES ('Dispatch Fixture', 'rss', 'https://fixture.invalid', "
            "'https://fixture.invalid/rss', true) RETURNING id"
        )
    engine = create_async_engine(system_database.url("vector_collect", sqlalchemy=True))
    try:
        yield (
            ids,
            unknown_id,
            SourceDispatchService(async_sessionmaker(engine, expire_on_commit=False)),
        )
    finally:
        await engine.dispose()


def dispatcher_for(selection, actions, calls, waits):
    scripted = {key: deque(value) for key, value in actions.items()}

    async def sleep(seconds):
        waits.append(seconds)

    return SourceAcquisitionDispatcher(
        dispatch_service=selection,
        sender_factory=lambda: open_acquisition_sender(
            client_factory=lambda: ScriptedSqsClient(scripted, calls),
            queue_url=QUEUE_URL,
        ),
        sleep=sleep,
        jitter=lambda: 0.5,
    )


def sent_source_ids(calls):
    return [json.loads(body)["source_id"] for _, body in calls]


@pytest.mark.asyncio
async def test_requests_follow_current_active_registered_sources(
    system_database, acquisition_sources
):
    """DBの現状態から依頼を生成し、成功済みを除いた再送で受付を完了する。"""
    ids, unknown_id, selection = acquisition_sources
    a, b = ids["Engadget"], ids["TechCrunch"]
    calls, waits = [], []
    dispatcher = dispatcher_for(
        selection,
        {
            a: [None, None],
            b: [
                service_error("ServiceUnavailable", 503),
                ReadTimeoutError(endpoint_url=QUEUE_URL),
                None,
            ],
        },
        calls,
        waits,
    )
    schedule = SourceAcquisitionSchedule.model_validate(SCHEDULE)
    with capture_logs() as logs:
        await dispatcher.dispatch(schedule)
    assert sent_source_ids(calls) == [a, b, b, b]
    assert all(queue == QUEUE_URL for queue, _ in calls)
    assert json.loads(calls[0][1]) == {
        "request_id": f"high/2026-09-13T01:00:00Z/{a}",
        "cadence": "high",
        "scheduled_at": "2026-09-13T01:00:00Z",
        "source_id": a,
    }
    assert calls[1][1] == calls[2][1] == calls[3][1]
    assert json.loads(calls[1][1])["request_id"] == f"high/2026-09-13T01:00:00Z/{b}"
    assert waits == [0.5, 1.0]
    assert any(log["event"] == "dispatch_source_unknown" for log in logs)
    assert not any(log["event"] == "source_acquisition_send_failed" for log in logs)

    async with system_database.connect("vector") as db:
        await db.execute("UPDATE news_sources SET is_active = false WHERE id = $1", b)
    await dispatcher.dispatch(schedule)
    assert sent_source_ids(calls) == [a, b, b, b, a]
    assert calls[-1][1] == calls[0][1]

    async with system_database.connect("vector") as db:
        await db.execute("UPDATE news_sources SET is_active = false WHERE id = $1", a)
    with capture_logs() as excluded_logs:
        await dispatcher.dispatch(schedule)
    assert len(calls) == 5
    assert any(log["event"] == "dispatch_source_unknown" for log in excluded_logs)

    async with system_database.connect("vector") as db:
        await db.execute(
            "UPDATE news_sources SET is_active = false WHERE id = $1", unknown_id
        )
    with capture_logs() as empty_logs:
        await dispatcher.dispatch(schedule)
    assert len(calls) == 5
    assert not empty_logs


@pytest.mark.asyncio
async def test_unsent_requests_fail_after_three_attempts(acquisition_sources):
    """回復可能でも3回で打ち切り、未送信を記録して投入失敗を返す。"""
    ids, _, selection = acquisition_sources
    a, b = ids["Engadget"], ids["TechCrunch"]
    calls, waits = [], []
    dispatcher = dispatcher_for(
        selection,
        {a: [None], b: [service_error("RequestThrottled") for _ in range(3)]},
        calls,
        waits,
    )
    with capture_logs() as logs, pytest.raises(AcquisitionDispatchError) as caught:
        await dispatcher.dispatch(SourceAcquisitionSchedule.model_validate(SCHEDULE))
    assert sent_source_ids(calls) == [a, b, b, b]
    assert [(item.request_id, item.attempts) for item in caught.value.unsent] == [
        (f"high/2026-09-13T01:00:00Z/{b}", 3)
    ]
    failures = [log for log in logs if log["event"] == "source_acquisition_send_failed"]
    assert len(failures) == 1
    assert failures[0]["attempts"] == 3
    assert failures[0]["service_code"] == "RequestThrottled"
    assert "private-sdk-message" not in repr(logs)
    assert caught.value.__context__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure, expected_kind",
    [
        pytest.param(
            service_error("AccessDenied", 503),
            "request_rejected",
            id="known-rejection-before-status",
        ),
        pytest.param(
            service_error("NewServiceError"), "unknown_service_error", id="unknown-code"
        ),
        pytest.param({}, "invalid_response", id="missing-response-fields"),
        pytest.param(
            {"MessageId": "id", "MD5OfMessageBody": "0" * 32},
            "body_mismatch",
            id="body-mismatch",
        ),
    ],
)
async def test_non_retryable_failure_does_not_stop_other_sources(
    acquisition_sources, failure, expected_kind
):
    """再送対象外の依頼を残しても他の依頼は完了し、最後に投入失敗を返す。"""
    ids, _, selection = acquisition_sources
    a, b = ids["Engadget"], ids["TechCrunch"]
    calls, waits = [], []
    dispatcher = dispatcher_for(
        selection,
        {a: [failure], b: [service_error("ServiceUnavailable", 503), None]},
        calls,
        waits,
    )
    with capture_logs() as logs, pytest.raises(AcquisitionDispatchError) as caught:
        await dispatcher.dispatch(SourceAcquisitionSchedule.model_validate(SCHEDULE))
    assert sent_source_ids(calls) == [a, b, b]
    assert [
        (item.request_id, item.attempts, item.failure.kind)
        for item in caught.value.unsent
    ] == [(f"high/2026-09-13T01:00:00Z/{a}", 1, expected_kind)]
    failures = [log for log in logs if log["event"] == "source_acquisition_send_failed"]
    assert len(failures) == 1
    assert caught.value.unsent[0].failure.disposition.value == (
        "non_retryable"
        if expected_kind == "request_rejected"
        else "investigation_required"
    )
    assert failures[0]["failure_kind"] == expected_kind
    assert "private-sdk-message" not in repr(logs)
    assert QUEUE_URL not in repr(logs)
