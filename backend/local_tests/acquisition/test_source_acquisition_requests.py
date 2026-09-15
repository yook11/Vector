"""予定回から実DBの対象を選び、失敗した取得依頼だけを再送する。"""

import asyncio
import json
from collections import deque
from dataclasses import dataclass
from hashlib import md5
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError
from structlog.testing import capture_logs

from app.lambda_handlers.source_dispatch import handler as entrypoint
from app.lambda_handlers.source_dispatch import resources
from app.lambda_handlers.source_dispatch.failure import SourceDispatchLambdaError
from tests.iam_fixtures import inject_test_db_signer

QUEUE_URL = "https://sqs.ap-northeast-1.amazonaws.com/123456789012/source-acquisitions"
SCHEDULE = {"cadence": "high", "scheduled_at": "2026-09-13T10:00:00+09:00"}
SCHEDULED_AT = "2026-09-13T01:00:00Z"

pytestmark = pytest.mark.asyncio


def service_error(code, status=400):
    return ClientError(
        {
            "Error": {"Code": code, "Message": "private-sdk-message"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "SendMessage",
    )


def expected_request(source_id):
    return {
        "request_id": f"high/{SCHEDULED_AT}/{source_id}",
        "cadence": "high",
        "scheduled_at": SCHEDULED_AT,
        "source_id": source_id,
    }


def send_failed_logs(logs):
    return [log for log in logs if log["event"] == "source_acquisition_send_failed"]


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


@dataclass
class DispatchRuntime:
    invoke: object
    calls: list
    waits: list
    sqs_clients: list

    @property
    def sent_source_ids(self):
        return [json.loads(body)["source_id"] for _, body in self.calls]

    def request(self, index):
        return json.loads(self.calls[index][1])


@pytest.fixture
async def acquisition_sources(system_database):
    """high一致・cadence不一致・未登録の対照を残し、他は無効化する。"""
    async with system_database.connect("vector") as db:
        await db.execute("UPDATE news_sources SET is_active = false")
        rows = await db.fetch(
            "UPDATE news_sources SET is_active = true "
            "WHERE name IN ('Engadget', 'TechCrunch', 'OpenAI') RETURNING id, name"
        )
        ids = {row["name"]: row["id"] for row in rows}
        assert set(ids) == {"Engadget", "TechCrunch", "OpenAI"}
        unregistered = await db.fetchval(
            "INSERT INTO news_sources "
            "(name, source_type, site_url, endpoint_url, is_active) "
            "VALUES ('Dispatch Fixture', 'rss', 'https://fixture.invalid', "
            "'https://fixture.invalid/rss', true) RETURNING id"
        )
    return SimpleNamespace(
        high=ids["Engadget"],
        other_high=ids["TechCrunch"],
        medium=ids["OpenAI"],
        unregistered=unregistered,
    )


@pytest.fixture
def deactivate_source(system_database):
    async def deactivate(source_id):
        async with system_database.connect("vector") as db:
            await db.execute(
                "UPDATE news_sources SET is_active = false WHERE id = $1",
                source_id,
            )

    return deactivate


@pytest.fixture
def start_dispatch(system_database, monkeypatch):
    """接続設定とAWS境界を準備し、指定したSQS応答で入口を実行する。"""
    database_url = inject_test_db_signer(
        monkeypatch,
        system_database.url("vector_collect", sqlalchemy=True),
        resources_module=resources,
    )
    rds_session = resources.Session
    for key, value in {
        "ENV": "test",
        "DATABASE_URL": database_url,
        "DB_IAM_AUTH": "true",
        "AWS_REGION": "ap-northeast-1",
        "SQS_SOURCE_ACQUISITION_QUEUE_URL": QUEUE_URL,
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(entrypoint, "setup_lambda_logging", lambda: None)

    def start(actions):
        scripted = {key: deque(value) for key, value in actions.items()}
        calls, waits, clients = [], [], []

        def create_client(service, **kwargs):
            if service == "sqs":
                client = ScriptedSqsClient(scripted, calls)
                clients.append(client)
                return client
            return rds_session().create_client(service, **kwargs)

        async def sleep(seconds):
            waits.append(seconds)

        async def invoke():
            return await asyncio.to_thread(entrypoint.handler, SCHEDULE, None)

        monkeypatch.setattr(
            resources, "Session", lambda: SimpleNamespace(create_client=create_client)
        )
        monkeypatch.setattr(resources, "sleep", sleep)
        monkeypatch.setattr(resources, "random", lambda: 0.5)
        return DispatchRuntime(
            invoke=invoke, calls=calls, waits=waits, sqs_clients=clients
        )

    return start


async def test_dispatch_selects_active_registered_sources_for_the_schedule(
    acquisition_sources, start_dispatch
):
    """投入は、実行開始時点のDBで有効かつ登録済みで、予定回の頻度と一致するソースだけに行う。"""
    sources = acquisition_sources
    runtime = start_dispatch({sources.high: [None], sources.other_high: [None]})
    with capture_logs() as logs:
        await runtime.invoke()
    assert runtime.sent_source_ids == [sources.high, sources.other_high]
    assert sources.medium not in runtime.sent_source_ids
    assert sources.unregistered not in runtime.sent_source_ids
    assert all(queue == QUEUE_URL for queue, _ in runtime.calls)
    assert runtime.request(0) == expected_request(sources.high)
    assert runtime.request(1) == expected_request(sources.other_high)
    assert any(log["event"] == "dispatch_source_unknown" for log in logs)


async def test_dispatch_skips_sqs_when_no_matching_source(
    acquisition_sources, start_dispatch, deactivate_source
):
    """一致する対象がなければ送信せず、SQSクライアントも開かない。"""
    sources = acquisition_sources
    await deactivate_source(sources.high)
    await deactivate_source(sources.other_high)
    runtime = start_dispatch({})
    with capture_logs() as logs:
        await runtime.invoke()
    assert runtime.sent_source_ids == []
    assert any(log["event"] == "dispatch_source_unknown" for log in logs)
    assert runtime.sqs_clients == []


async def test_invalid_source_name_does_not_stop_other_dispatch_targets(
    system_database, acquisition_sources, start_dispatch
):
    """DB上の不正名を個別の棄却にし、登録済みソースの投入を継続する。"""
    sources = acquisition_sources
    async with system_database.connect("vector") as db:
        await db.execute(
            "UPDATE news_sources SET name='!!!' WHERE id=$1", sources.unregistered
        )
    runtime = start_dispatch({sources.high: [None], sources.other_high: [None]})
    with capture_logs() as logs:
        await runtime.invoke()
    assert runtime.sent_source_ids == [sources.high, sources.other_high]
    assert any(
        log["event"] == "dispatch_source_name_invalid"
        and log["source_id"] == sources.unregistered
        for log in logs
    )


async def test_retryable_failure_is_resent_until_accepted(
    acquisition_sources, start_dispatch
):
    """成功済みは再送せず、失敗した依頼だけを同じ本文で送り直して受付する。"""
    sources = acquisition_sources
    runtime = start_dispatch(
        {
            sources.high: [None],
            sources.other_high: [
                service_error("ServiceUnavailable", 503),
                ReadTimeoutError(endpoint_url=QUEUE_URL),
                None,
            ],
        }
    )
    with capture_logs() as logs:
        await runtime.invoke()
    assert runtime.sent_source_ids == [
        sources.high,
        sources.other_high,
        sources.other_high,
        sources.other_high,
    ]
    assert runtime.calls[1][1] == runtime.calls[2][1] == runtime.calls[3][1]
    assert runtime.request(1) == expected_request(sources.other_high)
    assert runtime.waits == [0.5, 1.0]
    assert not send_failed_logs(logs)


async def test_unsent_requests_fail_after_three_attempts(
    acquisition_sources, start_dispatch
):
    """回復可能でも3回で打ち切り、未送信を残して投入失敗にする。"""
    sources = acquisition_sources
    runtime = start_dispatch(
        {
            sources.high: [None],
            sources.other_high: [service_error("RequestThrottled") for _ in range(3)],
        }
    )
    with capture_logs() as logs, pytest.raises(SourceDispatchLambdaError) as caught:
        await runtime.invoke()
    assert runtime.sent_source_ids == [
        sources.high,
        sources.other_high,
        sources.other_high,
        sources.other_high,
    ]
    assert caught.value.phase == "dispatch"
    assert caught.value.error_class.endswith(".AcquisitionDispatchError")
    failures = send_failed_logs(logs)
    assert len(failures) == 1
    assert (
        failures[0]["request_id"] == expected_request(sources.other_high)["request_id"]
    )
    assert failures[0]["attempts"] == 3
    assert failures[0]["service_code"] == "RequestThrottled"
    assert "private-sdk-message" not in repr(logs)
    assert caught.value.__suppress_context__ is True


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
    acquisition_sources, start_dispatch, failure, expected_kind
):
    """再送しない失敗が残っても他の依頼は完了し、最後に投入失敗にする。"""
    sources = acquisition_sources
    runtime = start_dispatch(
        {
            sources.high: [failure],
            sources.other_high: [service_error("ServiceUnavailable", 503), None],
        }
    )
    with capture_logs() as logs, pytest.raises(SourceDispatchLambdaError) as caught:
        await runtime.invoke()
    assert runtime.sent_source_ids == [
        sources.high,
        sources.other_high,
        sources.other_high,
    ]
    assert caught.value.phase == "dispatch"
    assert caught.value.error_class.endswith(".AcquisitionDispatchError")
    failures = send_failed_logs(logs)
    assert len(failures) == 1
    assert failures[0]["request_id"] == expected_request(sources.high)["request_id"]
    assert failures[0]["attempts"] == 1
    assert failures[0]["failure_kind"] == expected_kind
    assert "private-sdk-message" not in repr(logs)
    assert QUEUE_URL not in repr(logs)
