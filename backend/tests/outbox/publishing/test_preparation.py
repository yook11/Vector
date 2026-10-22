"""DB・通信を使わず、実イベント契約と本文生成モックで変換の境界を検証する。"""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import Mock
from uuid import UUID

import pytest

from app.analysis.assessment.events import AssessedEventInvalidError
from app.collection.events import (
    AnalyzableEventInvalidError,
)
from app.outbox.publishing.assessed_in_scope import build_assessed_in_scope_message
from app.outbox.publishing.contracts import EventEnvelope, PublishFailed
from app.outbox.publishing.preparation import prepare_messages
from app.outbox.publishing.route import (
    EventDeliveryRoute,
    EventMessage,
    EventRouteMismatchError,
)
from app.shared.event_contract import (
    EventInvalid,
    EventInvalidReason,
)

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


@pytest.mark.parametrize(
    "case,exception",
    [
        ("empty", ValueError),
        ("eleven", ValueError),
        ("duplicate", ValueError),
        ("none", TypeError),
        ("string", TypeError),
        ("iterator", TypeError),
        ("element", TypeError),
        ("event_id", TypeError),
        ("event_type", TypeError),
        ("schema_version", TypeError),
        ("occurred_at", TypeError),
        ("payload", TypeError),
    ],
)
def test_call_contract_violations_precede_message_building(
    envelope, route, case, exception
):
    """入力契約違反を本文生成・SDK初期化の前に拒否する。"""
    inputs = {
        "empty": [],
        "eleven": [replace(envelope, event_id=UUID(int=i)) for i in range(11)],
        "duplicate": [envelope, envelope],
        "none": None,
        "string": "private",
        "iterator": iter([envelope]),
        "element": [object()],
        "event_id": [replace(envelope, event_id="private")],
        "event_type": [replace(envelope, event_type=1)],
        "schema_version": [replace(envelope, schema_version=True)],
        "occurred_at": [replace(envelope, occurred_at="private")],
        "payload": [replace(envelope, payload=[])],
    }
    builder = Mock()
    with pytest.raises(exception):
        prepare_messages(inputs[case], route=route, build_message=builder)
    builder.assert_not_called()


def test_input_count_is_checked_before_excluding_invalid_events(envelope, route):
    """不正イベントを除けば10件でも、元の11件を本文生成前に拒否する。"""
    events = [replace(envelope, event_id=UUID(int=index)) for index in range(11)]
    events[0] = replace(events[0], event_type="unsupported")
    builder = Mock()
    with pytest.raises(ValueError):
        prepare_messages(events, route=route, build_message=builder)
    builder.assert_not_called()


def test_all_invalid_events_return_only_failures(envelope, route):
    """全イベントの内容が不正なら、送信対象を空にして個別失敗を返す。"""
    messages, failures = prepare_messages(
        [replace(envelope, schema_version=2)],
        route=route,
        build_message=build_assessed_in_scope_message,
    )
    assert messages == ()
    assert len(failures) == 1
    assert failures[0].event_id == envelope.event_id
    assert isinstance(failures[0].error, AssessedEventInvalidError)
    assert failures[0].error.invalid.reason == "unsupported_schema_version"


def test_invalid_event_is_excluded_without_changing_valid_message(envelope, route):
    """内容不正のイベントだけを除外し、正常なイベントの本文を保つ。"""
    invalid = replace(envelope, schema_version=2)
    good = replace(envelope, event_id=UUID(int=2))
    messages, failures = prepare_messages(
        [invalid, good],
        route=route,
        build_message=build_assessed_in_scope_message,
    )
    assert messages == (build_assessed_in_scope_message(good),)
    assert [failure.event_id for failure in failures] == [invalid.event_id]
    assert isinstance(failures[0].error, AssessedEventInvalidError)


def test_custom_route_preserves_messages_and_original_failure(envelope):
    """独自の配送種別と本文生成でも、正常本文の順序と元の検証例外を保持する。"""
    events = [
        replace(envelope, event_id=UUID(int=i), event_type="test.event")
        for i in range(1, 4)
    ]
    error = AnalyzableEventInvalidError(
        EventInvalid(reason=EventInvalidReason.INVALID_PAYLOAD, issues=())
    )
    expected = (
        EventMessage(events[1].event_id, "second body"),
        EventMessage(events[2].event_id, "third body"),
    )
    builder = Mock(side_effect=[error, *expected])
    messages, failures = prepare_messages(
        events,
        route=EventDeliveryRoute("test.event", QUEUE_URL),
        build_message=builder,
    )
    assert messages == expected
    assert len(failures) == 1
    assert failures[0].event_id == events[0].event_id
    assert failures[0].error is error
    assert [call.args[0] for call in builder.call_args_list] == events


def test_occurred_at_preserves_microseconds_in_utc(envelope, route):
    """本文の発生日時をUTCに正規化し、マイクロ秒を維持する。"""
    event = replace(
        envelope,
        occurred_at=datetime(
            2026,
            9,
            7,
            12,
            0,
            0,
            123456,
            tzinfo=timezone(timedelta(hours=9)),
        ),
    )
    messages, failures = prepare_messages(
        [event],
        route=route,
        build_message=build_assessed_in_scope_message,
    )
    assert failures == ()
    assert json.loads(messages[0].body) == {
        "event_id": str(event.event_id),
        "event_type": event.event_type,
        "schema_version": 1,
        "occurred_at": "2026-09-07T03:00:00.123456Z",
        "payload": event.payload,
    }


@pytest.mark.parametrize("case", ["wrong_type", "wrong_id"])
def test_builder_contract_violation_propagates(envelope, route, case):
    """本文生成の型・ID違反を個別失敗にせず呼び出し元へ伝える。"""
    message = {"wrong_type": object(), "wrong_id": EventMessage(UUID(int=2), "body")}[
        case
    ]
    with pytest.raises(TypeError, match="message must preserve input event ID"):
        prepare_messages(
            [envelope], route=route, build_message=Mock(return_value=message)
        )


def test_route_mismatch_precedes_message_building(envelope):
    """配送種別の不一致は本文を作らず、元イベントの個別失敗にする。"""
    builder = Mock()
    messages, failures = prepare_messages(
        [envelope],
        route=EventDeliveryRoute("different", QUEUE_URL),
        build_message=builder,
    )
    assert messages == ()
    assert len(failures) == 1
    assert isinstance(failures[0], PublishFailed)
    assert failures[0].event_id == envelope.event_id
    assert isinstance(failures[0].error, EventRouteMismatchError)
    builder.assert_not_called()


def test_unexpected_builder_exception_is_propagated(envelope, route):
    """途中の想定外例外は先行する変換結果も返さず、そのまま伝播する。"""
    second = replace(envelope, event_id=UUID(int=2))
    original = ValueError("unexpected")
    builder = Mock(side_effect=[EventMessage(envelope.event_id, "body"), original])
    with pytest.raises(ValueError) as caught:
        prepare_messages([envelope, second], route=route, build_message=builder)
    assert caught.value is original


def test_requires_message_builder(envelope, route):
    """本文生成関数が未指定なら入力を処理する前に拒否する。"""
    with pytest.raises(TypeError, match="requires a message builder"):
        prepare_messages([envelope], route=route, build_message=None)


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
    from app.outbox.publishing.route import EventDeliveryRoute

    args = dict(event_type=envelope.event_type, queue_url=QUEUE_URL)
    args[field] = value
    with pytest.raises(error):
        EventDeliveryRoute(**args)


def test_route_rejects_event_of_another_type(envelope):
    """配送先の対応は、自分の種別のイベントを通し、ほかの種別のイベントを種別の不一致として拒否する。"""
    from app.outbox.publishing.route import EventDeliveryRoute

    route = EventDeliveryRoute(envelope.event_type, QUEUE_URL)
    route.ensure_matches(envelope)
    with pytest.raises(EventRouteMismatchError):
        route.ensure_matches(replace(envelope, event_type="another.event"))
