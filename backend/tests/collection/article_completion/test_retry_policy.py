"""``app.collection.article_completion.retry_policy`` の純関数テスト。

policy table の値そのものはスナップショット (spec line 232-238) を反映した
だけなので、ここで再検証するのは無価値。代わりに **policy 選択ロジック**
と **次回 delay 計算の振る舞い** に絞る:

- exception 階層と policy の対応 (5 分岐)
- ``next_delay_minutes`` の境界 (attempt_count 範囲外、schedule 長を超える)
- ``Retry-After`` の上書きが ``compute_next_delay_minutes`` で適用される
- ``MAX_DELAY_MINUTES`` cap が override にも policy schedule にも適用される
"""

from __future__ import annotations

import pytest

from app.collection.article_completion.retry_policy import (
    BLIP_POLICY,
    MAX_DELAY_MINUTES,
    OUTAGE_POLICY,
    RETRY_AFTER_POLICY,
    TIMEOUT_POLICY,
    UNKNOWN_POLICY,
    RetryPolicy,
    compute_next_delay_minutes,
    retry_policy_for,
)
from app.collection.errors import (
    ReadTimeout,
    ServerErrorBlip,
    ServerErrorOutage,
    ServerErrorRetryAfter,
    TemporaryFetchError,
)


class TestRetryPolicyForExceptionHierarchy:
    """exception → policy の写像が一意かつ正しいことを保証。"""

    def test_blip_for_blip_class(self) -> None:
        policy, override = retry_policy_for(ServerErrorBlip("502: x"))
        assert policy is BLIP_POLICY
        assert override is None

    def test_outage_for_outage_class(self) -> None:
        policy, override = retry_policy_for(ServerErrorOutage("503: x"))
        assert policy is OUTAGE_POLICY
        assert override is None

    def test_timeout_for_read_timeout(self) -> None:
        policy, override = retry_policy_for(ReadTimeout("timeout: x"))
        assert policy is TIMEOUT_POLICY
        assert override is None

    def test_retry_after_returns_seconds_override(self) -> None:
        # 重要: server 指示秒数を override で返す (caller 優先)
        exc = ServerErrorRetryAfter("503 ra: x", retry_after_seconds=120.0)
        policy, override = retry_policy_for(exc)
        assert policy is RETRY_AFTER_POLICY
        assert override == 120.0

    def test_unknown_for_bare_temporary(self) -> None:
        # 素の TemporaryFetchError (未分類) は保守的な outage 寄りで吸収
        policy, override = retry_policy_for(TemporaryFetchError("x"))
        assert policy is UNKNOWN_POLICY
        assert override is None


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


class TestComputeNextDelayMinutesIntegration:
    """``compute_next_delay_minutes`` は cap と override を統合する責務。"""

    def test_blip_uses_schedule_no_cap_needed(self) -> None:
        # BLIP の schedule 値は全て 60 以下、cap は不発
        policy, delay = compute_next_delay_minutes(
            ServerErrorBlip("x"), attempt_count=1
        )
        assert policy is BLIP_POLICY
        assert delay == 0.5  # schedule[0]

    def test_retry_after_seconds_converted_to_minutes(self) -> None:
        # 120 秒 → 2 分。cap (60 分) は不発
        exc = ServerErrorRetryAfter("x", retry_after_seconds=120.0)
        policy, delay = compute_next_delay_minutes(exc, attempt_count=1)
        assert policy is RETRY_AFTER_POLICY
        assert delay == 2.0

    def test_retry_after_capped_at_max_delay(self) -> None:
        # server が 1 時間以上を指示しても 60 分で頭打ち
        exc = ServerErrorRetryAfter("x", retry_after_seconds=7200.0)  # 2 時間
        _, delay = compute_next_delay_minutes(exc, attempt_count=1)
        assert delay == MAX_DELAY_MINUTES  # 60 分

    def test_outage_schedule_includes_max_delay(self) -> None:
        # OUTAGE schedule の途中で 60 分が出る、cap と一致
        _, delay = compute_next_delay_minutes(ServerErrorOutage("x"), attempt_count=4)
        assert delay == 60.0  # schedule[3]
        # それ以上で攻めて attempt_count=99 でも 60 で頭打ち
        _, delay_99 = compute_next_delay_minutes(
            ServerErrorOutage("x"), attempt_count=99
        )
        assert delay_99 == 60.0

    def test_retry_after_zero_seconds_yields_zero_delay(self) -> None:
        # server が「すぐ retry」を指示した場合、0 分。cap も無効化されない
        exc = ServerErrorRetryAfter("x", retry_after_seconds=0.0)
        _, delay = compute_next_delay_minutes(exc, attempt_count=1)
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
