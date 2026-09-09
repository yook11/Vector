"""配信入力の不変条件を、DBに依存せず生成時に検証する。"""

from dataclasses import FrozenInstanceError
from datetime import timedelta

import pytest

from app.outbox.delivery.values import DeliveryBatchSelection, LeaseDuration, RetryDelay


@pytest.mark.parametrize("value", [None, 1, True])
def test_selection_rejects_non_string_event_type(value):
    """イベント種別は文字列への自動変換を行わない。"""
    with pytest.raises(TypeError):
        DeliveryBatchSelection(event_type=value, limit=10)


@pytest.mark.parametrize("value", ["", " \t\n"])
def test_selection_rejects_blank_event_type(value):
    """空の対象指定を生成時に拒否する。"""
    with pytest.raises(ValueError):
        DeliveryBatchSelection(event_type=value, limit=10)


@pytest.mark.parametrize("value", [True, False, 1.5, "10", None])
def test_selection_rejects_non_integer_limit(value):
    """boolを含めて整数以外の件数を拒否する。"""
    with pytest.raises(TypeError):
        DeliveryBatchSelection(event_type="test.outbox", limit=value)


@pytest.mark.parametrize("limit", [-1, 0, 10, 100])
def test_selection_preserves_event_type_and_nonpositive_limit(limit):
    """完全一致に使う文字列と、取得しない指定も補正せず保持する。"""
    selection = DeliveryBatchSelection(event_type=" test.outbox ", limit=limit)
    assert selection.event_type == " test.outbox "
    assert selection.limit == limit


@pytest.mark.parametrize("factory", [LeaseDuration, RetryDelay])
@pytest.mark.parametrize("value", [None, 1, True, "5 minutes"])
def test_durations_reject_non_timedelta(factory, value):
    """時間はtimedeltaからのみ生成できる。"""
    with pytest.raises(TypeError):
        factory(value)


@pytest.mark.parametrize("value", [timedelta(0), timedelta(microseconds=-1)])
def test_lease_duration_rejects_nonpositive_value(value):
    """lease期間は最小の負数とゼロも拒否する。"""
    with pytest.raises(ValueError):
        LeaseDuration(value)


def test_retry_delay_rejects_negative_value():
    """即時再試行より前の時刻を指定できない。"""
    with pytest.raises(ValueError):
        RetryDelay(timedelta(microseconds=-1))


@pytest.mark.parametrize("factory", [LeaseDuration, RetryDelay])
def test_durations_preserve_positive_value(factory):
    """期間の精度を落とさず保持する。"""
    value = timedelta(seconds=150, microseconds=1)
    assert factory(value).value == value


def test_retry_delay_accepts_zero():
    """待ち時間ゼロの即時再試行を許容する。"""
    assert RetryDelay(timedelta(0)).value == timedelta(0)


@pytest.mark.parametrize(
    "value,attribute,replacement",
    [
        (DeliveryBatchSelection(event_type="test.outbox", limit=10), "limit", True),
        (DeliveryBatchSelection(event_type="test.outbox", limit=10), "event_type", ""),
        (LeaseDuration(timedelta(seconds=1)), "value", timedelta(0)),
        (RetryDelay(timedelta(0)), "value", timedelta(seconds=-1)),
    ],
)
def test_validated_values_cannot_be_mutated(value, attribute, replacement):
    """生成後に不正な内容へ書き換えられない。"""
    with pytest.raises(FrozenInstanceError):
        setattr(value, attribute, replacement)
