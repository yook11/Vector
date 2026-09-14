"""再試行時刻の日時契約を確認する。"""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.collection.article_completion.retry_at import RetryAt


def test_normalizes_aware_time_to_utc():
    """同じ瞬間をUTCで保持する。"""
    value = datetime(2026, 9, 14, 9, tzinfo=timezone(timedelta(hours=9)))
    retry = RetryAt(value)
    assert retry.value == datetime(2026, 9, 14, tzinfo=UTC)
    assert retry.value.tzinfo is UTC


def test_rejects_naive_time():
    """タイムゾーンのない日時を暗黙に解釈しない。"""
    with pytest.raises(ValueError):
        RetryAt(datetime(2026, 9, 14))


@pytest.mark.parametrize("elapsed", [timedelta(), timedelta(seconds=1)])
def test_expired_time_has_zero_remaining_without_changing_original(elapsed):
    """期限経過後も元の日時を保持し、残り時間だけを0にする。"""
    value = datetime(2026, 9, 14, tzinfo=UTC)
    retry = RetryAt(value)
    assert retry.remaining(value + elapsed) == timedelta()
    assert retry.value == value


def test_remaining_retains_fractional_seconds():
    """値オブジェクトは配送側の秒単位へ丸めない。"""
    now = datetime(2026, 9, 14, tzinfo=UTC)
    retry = RetryAt(now + timedelta(seconds=1, microseconds=1))
    assert retry.remaining(now) == timedelta(seconds=1, microseconds=1)


def test_remaining_rejects_naive_clock():
    """計算の基準日時にもタイムゾーンを要求する。"""
    with pytest.raises(ValueError):
        RetryAt(datetime(2026, 9, 14, tzinfo=UTC)).remaining(datetime(2026, 9, 14))


def test_retry_time_cannot_be_reassigned():
    """分類済みの待機時刻は変更できない。"""
    retry = RetryAt(datetime(2026, 9, 14, tzinfo=UTC))
    with pytest.raises(FrozenInstanceError):
        retry.value = datetime(2026, 9, 15, tzinfo=UTC)
