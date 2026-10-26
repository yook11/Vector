"""DB・通信を使わず、実イベント契約と本文生成モックで1件の変換の境界を検証する。"""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import Mock
from uuid import UUID

import pytest

from app.analysis.assessment.events import AssessedEventInvalidError
from app.collection.events import AnalyzableEventInvalidError
from app.outbox.event.assessed_in_scope import build_assessed_in_scope_message
from app.outbox.event.envelope import EventEnvelope
from app.outbox.event.message import EventMessage
from app.outbox.relay.preparation import build_sendable_message
from app.outbox.relay.route import EventDeliveryRoute, EventRouteMismatchError
from app.shared.event_contract import EventInvalid, EventInvalidReason

QUEUE_URL = "https://sqs.invalid/embedding"


@pytest.fixture
def envelope():
    return EventEnvelope(
        event_id=UUID(int=1),
        event_type="article.assessed_in_scope",
        schema_version=1,
        occurred_at=datetime(2026, 9, 7, 3, tzinfo=UTC),
        payload={"curation_id": 123, "analyzed_article_id": 456},
    )


@pytest.fixture
def route(envelope):
    return EventDeliveryRoute(envelope.event_type, QUEUE_URL)


def test_valid_event_becomes_message_from_builder(envelope, route):
    """宛先と契約を満たすイベントは、本文生成関数が作った本文になる。"""
    message = EventMessage(envelope.event_id, "body")
    assert (
        build_sendable_message(
            envelope, route=route, build_message=Mock(return_value=message)
        )
        == message
    )


def test_occurred_at_preserves_microseconds_in_utc(envelope, route):
    """本文の発生日時をUTCに正規化し、マイクロ秒を維持する。"""
    event = replace(
        envelope,
        occurred_at=datetime(
            2026, 9, 7, 12, 0, 0, 123456, tzinfo=timezone(timedelta(hours=9))
        ),
    )
    message = build_sendable_message(
        event, route=route, build_message=build_assessed_in_scope_message
    )
    assert json.loads(message.body) == {
        "event_id": str(event.event_id),
        "event_type": event.event_type,
        "schema_version": 1,
        "occurred_at": "2026-09-07T03:00:00.123456Z",
        "payload": event.payload,
    }


def test_event_violating_contract_raises_contract_error(envelope, route):
    """版が契約と違うイベントは本文にせず、契約違反の例外を送出する。"""
    with pytest.raises(AssessedEventInvalidError) as caught:
        build_sendable_message(
            replace(envelope, schema_version=2),
            route=route,
            build_message=build_assessed_in_scope_message,
        )
    assert caught.value.invalid.reason == EventInvalidReason.UNSUPPORTED_SCHEMA_VERSION


@pytest.mark.parametrize(
    "error",
    [
        AnalyzableEventInvalidError(
            EventInvalid(reason=EventInvalidReason.INVALID_PAYLOAD, issues=())
        ),
        ValueError("unexpected"),
    ],
)
def test_builder_error_is_raised_unchanged(envelope, route, error):
    """本文生成関数の例外は、契約違反も想定外の例外も包み直さずに送出する。"""
    with pytest.raises(type(error)) as caught:
        build_sendable_message(
            envelope, route=route, build_message=Mock(side_effect=error)
        )
    assert caught.value is error


@pytest.mark.parametrize("case", ["wrong_type", "wrong_id"])
def test_builder_contract_violation_propagates(envelope, route, case):
    """本文生成の型・ID違反を送れない失敗にせず呼び出し元へ伝える。"""
    message = {"wrong_type": object(), "wrong_id": EventMessage(UUID(int=2), "body")}[
        case
    ]
    with pytest.raises(TypeError, match="message must preserve input event ID"):
        build_sendable_message(
            envelope, route=route, build_message=Mock(return_value=message)
        )


def test_event_of_another_type_is_rejected_before_building(envelope):
    """配送先と種別が違うイベントは、本文を作らずに種別の不一致を送出する。"""
    builder = Mock()
    with pytest.raises(EventRouteMismatchError):
        build_sendable_message(
            envelope,
            route=EventDeliveryRoute("different", QUEUE_URL),
            build_message=builder,
        )
    builder.assert_not_called()


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("event_type", " ", ValueError),
        ("queue_url", "\t", ValueError),
        ("event_type", None, TypeError),
        ("queue_url", None, TypeError),
    ],
)
def test_route_requires_explicit_nonblank_fields(envelope, field, value, error):
    """配送先と種別を必須とし、未設定を既定値で補わない。"""
    from app.outbox.relay.route import EventDeliveryRoute

    args = dict(event_type=envelope.event_type, queue_url=QUEUE_URL)
    args[field] = value
    with pytest.raises(error):
        EventDeliveryRoute(**args)


def test_route_rejects_event_of_another_type(envelope):
    """配送先の対応は、自分の種別のイベントを通し、ほかの種別のイベントを種別の不一致として拒否する。"""
    from app.outbox.relay.route import EventDeliveryRoute

    route = EventDeliveryRoute(envelope.event_type, QUEUE_URL)
    route.ensure_matches(envelope)
    with pytest.raises(EventRouteMismatchError):
        route.ensure_matches(replace(envelope, event_type="another.event"))
