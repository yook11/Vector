"""``app.collection.article_completion.retry_policy`` の純関数テスト。

policy table の値そのものはスナップショット (spec line 232-238) を反映した
だけなので、ここで再検証するのは無価値。代わりに **policy データの振る舞い**
に絞る (exception → policy の写像は disposition.py の関心に移管済、
``test_article_completion_disposition.py`` が網羅する):

- ``next_delay_minutes`` の境界 (attempt_count 範囲外、schedule 長を超える)
- ``effective_delay_minutes`` の retry_after 優先 / 分換算 / ``MAX_DELAY_MINUTES`` cap
- policy ごとの ``max_attempts`` (Service の exhausted 判定契約)
"""

from __future__ import annotations

import pytest

from app.collection.article_completion.retry_policy import (
    BLIP_POLICY,
    MAX_DELAY_MINUTES,
    OUTAGE_POLICY,
    RETRY_AFTER_POLICY,
    UNKNOWN_POLICY,
    RetryPolicy,
    effective_delay_minutes,
)


class TestNextDelayMinutesBoundaries:
    """``RetryPolicy.next_delay_minutes`` の境界条件。"""

    def test_attempt_count_one_returns_first_schedule(self) -> None:
        # 1 回目失敗 (lease 取得後の最初の attempt) は schedule[0]
        first = BLIP_POLICY.delay_minutes_schedule[0]
        assert BLIP_POLICY.next_delay_minutes(1) == first

    def test_attempt_beyond_schedule_uses_last(self) -> None:
        # schedule 長を超えても末尾値を返す (= 定常状態)
        last = BLIP_POLICY.delay_minutes_schedule[-1]
        assert BLIP_POLICY.next_delay_minutes(99) == last

    def test_attempt_zero_rejected(self) -> None:
        # 0-indexed の混入は意味的に bug、early fail させる
        with pytest.raises(ValueError, match="attempt_count must be >= 1"):
            BLIP_POLICY.next_delay_minutes(0)

    def test_negative_attempt_rejected(self) -> None:
        with pytest.raises(ValueError, match="attempt_count must be >= 1"):
            BLIP_POLICY.next_delay_minutes(-1)

    def test_empty_schedule_rejected(self) -> None:
        # 空 schedule の policy は構築可能でも next_delay 呼び出し時に detect
        empty = RetryPolicy(code="bad", max_attempts=1, delay_minutes_schedule=())
        with pytest.raises(ValueError, match="empty delay_minutes_schedule"):
            empty.next_delay_minutes(1)


class TestEffectiveDelayMinutes:
    """policy データ駆動の遅延算出 (disposition 経路の正準 API)。

    exception 型に依存せず policy 値だけで完結する。cap / override の責務を
    retry_after 経路 / schedule 経路の両軸で固定する。
    """

    def test_uses_policy_schedule_when_no_retry_after(self) -> None:
        # retry_after なし → policy.next_delay_minutes(attempt_count)
        delay = effective_delay_minutes(
            BLIP_POLICY, retry_after_seconds=None, attempt_count=1
        )
        assert delay == BLIP_POLICY.delay_minutes_schedule[0]

    def test_retry_after_seconds_converted_to_minutes(self) -> None:
        # 120 秒 → 2 分。policy schedule より優先される。
        delay = effective_delay_minutes(
            OUTAGE_POLICY, retry_after_seconds=120.0, attempt_count=1
        )
        assert delay == 2.0

    def test_retry_after_capped_at_max_delay(self) -> None:
        # server が 2 時間を指示しても 60 分で頭打ち。
        delay = effective_delay_minutes(
            RETRY_AFTER_POLICY, retry_after_seconds=7200.0, attempt_count=1
        )
        assert delay == MAX_DELAY_MINUTES

    def test_policy_schedule_capped_at_max_delay(self) -> None:
        # schedule 値が MAX 超なら cap が schedule 側にも効く (min clamp 対象)。
        hot = RetryPolicy(code="hot", max_attempts=3, delay_minutes_schedule=(120.0,))
        delay = effective_delay_minutes(hot, retry_after_seconds=None, attempt_count=1)
        assert delay == MAX_DELAY_MINUTES

    def test_retry_after_zero_yields_zero_delay(self) -> None:
        # server の即時 retry 指示は 0 分 (cap で潰さない)。
        delay = effective_delay_minutes(
            RETRY_AFTER_POLICY, retry_after_seconds=0.0, attempt_count=1
        )
        assert delay == 0.0


class TestPolicyMaxAttempts:
    """exhausted 判定が policy ごとに違うことを保証 (Service 側の caller 契約)。"""

    def test_blip_max_attempts_8(self) -> None:
        # blip = 短期粘り、8 回で諦め
        assert BLIP_POLICY.max_attempts == 8

    def test_outage_max_attempts_12(self) -> None:
        # outage = 長期粘り、12 回 (約 10 時間相当の schedule)
        assert OUTAGE_POLICY.max_attempts == 12

    def test_unknown_more_conservative_than_blip(self) -> None:
        # 未分類は blip より粘らない方が安全 (なぜなら原因不明)
        assert UNKNOWN_POLICY.max_attempts < OUTAGE_POLICY.max_attempts
