"""配信担当を確保する期間の不変条件を、DBに依存せず生成時に検証する。"""

from dataclasses import FrozenInstanceError
from datetime import timedelta

import pytest

from app.outbox.relay.repository import LeaseDuration


@pytest.mark.parametrize("value", [None, 1, True, "5 minutes"])
def test_lease_duration_rejects_non_timedelta(value):
    """時間はtimedeltaからのみ生成できる。"""
    with pytest.raises(TypeError):
        LeaseDuration(value)


@pytest.mark.parametrize("value", [timedelta(0), timedelta(microseconds=-1)])
def test_lease_duration_rejects_nonpositive_value(value):
    """lease期間は最小の負数とゼロも拒否する。"""
    with pytest.raises(ValueError):
        LeaseDuration(value)


def test_lease_duration_preserves_positive_value():
    """期間の精度を落とさず保持する。"""
    value = timedelta(seconds=150, microseconds=1)
    assert LeaseDuration(value).value == value


def test_lease_duration_cannot_be_mutated():
    """生成後に不正な内容へ書き換えられない。"""
    with pytest.raises(FrozenInstanceError):
        setattr(LeaseDuration(timedelta(seconds=1)), "value", timedelta(0))
