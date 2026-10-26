"""配信入力の不変条件を、DBに依存せず生成時に検証する。"""

from dataclasses import FrozenInstanceError
from datetime import timedelta

import pytest

from app.outbox.relay.values import LeaseDuration, RetryDelay


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
        (LeaseDuration(timedelta(seconds=1)), "value", timedelta(0)),
        (RetryDelay(timedelta(0)), "value", timedelta(seconds=-1)),
    ],
)
def test_validated_values_cannot_be_mutated(value, attribute, replacement):
    """生成後に不正な内容へ書き換えられない。"""
    with pytest.raises(FrozenInstanceError):
        setattr(value, attribute, replacement)
