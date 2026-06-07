"""``app.collection.article_completion.retry_policy`` の遅延原始型テスト。

検証対象は遅延 sum 型の振る舞い (誰が次回 ``ready_at`` を決めるか) と、再投入
テンプレートが運ぶ cap 契約:

- ``ScheduleDelay.minutes`` の段階遅延 (attempt 1→[0] / 超過→末尾 / cap / 範囲外 raise)
- ``FixedDelay.minutes`` の server 指示 (秒→分 / attempt 無視 / cap / 0)
- ``RetrySchedule`` の cap (Service の exhausted 判定契約)

exception → schedule の写像は ``test_article_completion_scrape_failure.py`` が所有する。
"""

from __future__ import annotations

import pytest

from app.collection.article_completion.retry_policy import (
    BLIP,
    MAX_DELAY_MINUTES,
    OUTAGE,
    TIMEOUT,
    UNKNOWN,
    FixedDelay,
    ScheduleDelay,
)


class TestScheduleDelay:
    """attempt_count で段階遅延する schedule 駆動の delay。"""

    def test_attempt_one_returns_first_entry(self) -> None:
        # 1 回目失敗 (claim 後の最初の attempt) は schedule[0]
        delay = ScheduleDelay((1.0, 2.0, 3.0))
        assert delay.minutes(1) == 1.0

    def test_attempt_beyond_schedule_uses_last(self) -> None:
        # schedule 長を超えたら末尾値で定常 (= 飽和遅延)
        delay = ScheduleDelay((1.0, 2.0, 3.0))
        assert delay.minutes(99) == 3.0

    def test_value_capped_at_max_delay(self) -> None:
        # schedule 値が MAX 超でも cap が効く (min clamp)
        delay = ScheduleDelay((MAX_DELAY_MINUTES + 60.0,))
        assert delay.minutes(1) == MAX_DELAY_MINUTES

    def test_attempt_below_one_rejected(self) -> None:
        # 0-indexed の混入は意味的に bug、early fail させる
        delay = ScheduleDelay((1.0,))
        with pytest.raises(ValueError, match="attempt_count must be >= 1"):
            delay.minutes(0)

    def test_empty_schedule_rejected_at_construction(self) -> None:
        # 空 schedule は呼び出し時でなく構築時に弾く (不変条件を型に固定)
        with pytest.raises(ValueError, match="minutes_schedule must not be empty"):
            ScheduleDelay(())


class TestFixedDelay:
    """server の ``Retry-After`` 指示を表す attempt 非依存の delay。"""

    def test_seconds_converted_to_minutes(self) -> None:
        # 120 秒 → 2 分
        assert FixedDelay(120.0).minutes(1) == 2.0

    def test_ignores_attempt_count(self) -> None:
        # server 指示は何回目でも同じ (attempt で揺れない)
        fixed = FixedDelay(120.0)
        assert fixed.minutes(1) == fixed.minutes(99)

    def test_value_capped_at_max_delay(self) -> None:
        # server が 2 時間を指示しても 60 分で頭打ち
        assert FixedDelay(7200.0).minutes(1) == MAX_DELAY_MINUTES

    def test_zero_yields_zero_delay(self) -> None:
        # 即時 retry 指示は 0 分 (cap で潰さない)
        assert FixedDelay(0.0).minutes(1) == 0.0


class TestRetryScheduleCaps:
    """再投入テンプレートが運ぶ cap は Service の exhausted 判定契約 (spec 由来)。"""

    def test_blip_caps_at_eight(self) -> None:
        # blip = 短期粘り、8 回で諦め (spec)
        assert BLIP.max_attempts == 8

    def test_outage_caps_at_twelve(self) -> None:
        # outage = 長期粘り、12 回 (約 10 時間相当の schedule, spec)
        assert OUTAGE.max_attempts == 12

    def test_unknown_more_conservative_than_outage(self) -> None:
        # 原因不明は長く粘らない方が安全 (設計意図)
        assert UNKNOWN.max_attempts < OUTAGE.max_attempts

    def test_timeout_matches_blip_horizon(self) -> None:
        # timeout も transient 想定で blip と同じ粘り幅 (設計意図)
        assert TIMEOUT.max_attempts == BLIP.max_attempts
