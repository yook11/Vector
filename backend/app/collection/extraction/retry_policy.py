"""エラー種別ごとの retry policy 純関数モジュール。

PR2.5-B 設計の核: ``ContentFetchService`` が ``TemporaryFetchError`` 系の
exception を受けたとき、policy 計算 (次回 ``ready_at`` の遅延と
最大試行回数) は本モジュールの純関数だけで完結し、Service 本体は
DB 状態更新と audit 焼付に専念する。

policy table の根拠は ``specs/pipeline-events-stage2-design.md`` line 226-244:

| エラー | delay schedule (分) | max attempts | 性質 |
|---|---|---|---|
| ConnectionError / 502 / 504 | 0.5 → 1 → 2 → 5 × 5 | 8 | blip-class |
| HTTP 503 (no Retry-After) | 5 → 15 → 30 → 60 × 9 | 12 | outage-class |
| HTTP 503 with Retry-After | header 値、後続 cap 60 分 | 12 | server-instructed |
| Read timeout | 2 → 5 × 7 | 8 | timeout (blip 寄り) |
| 未分類 TemporaryFetchError | 5 → 15 → 30 → 60 × 3 | 6 | unknown (outage 寄り保守的) |

policy 値は spec の table を実装したスナップショット。運用観察後の
調整は **PR2.5-D** で行う (本 PR では table の値を変えない)。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.collection.errors import (
    ReadTimeout,
    ServerErrorBlip,
    ServerErrorOutage,
    ServerErrorRetryAfter,
    TemporaryFetchError,
)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """policy 1 つを表す純データ。``next_delay_minutes`` は副作用なし。"""

    code: str
    """audit ``reason_code`` suffix (例: ``blip`` → ``temporary_will_retry_blip``)."""

    max_attempts: int
    """この attempt 番号以上で exhausted 扱い (policy ごとに異なる)。"""

    delay_minutes_schedule: tuple[float, ...]
    """attempt_count (1-indexed) ごとの遅延分。長さを超えたら末尾を使う。"""

    def next_delay_minutes(self, attempt_count: int) -> float:
        """``attempt_count`` 番目の試行が失敗した直後に使う next delay。

        ``attempt_count`` は **lease 取得後の今回の試行番号** (1-indexed)。
        1 回目失敗なら schedule[0] を返す。schedule 長を超えたら末尾を返す
        (e.g. blip の 5 分定常状態)。
        """
        if attempt_count < 1:
            raise ValueError(f"attempt_count must be >= 1, got {attempt_count}")
        if not self.delay_minutes_schedule:
            raise ValueError(f"empty delay_minutes_schedule for policy {self.code!r}")
        idx = min(attempt_count - 1, len(self.delay_minutes_schedule) - 1)
        return self.delay_minutes_schedule[idx]


# spec line 232-238 のテーブルに対応した policy 値。値の変更は PR2.5-D で。
BLIP_POLICY = RetryPolicy(
    code="blip",
    max_attempts=8,
    delay_minutes_schedule=(0.5, 1.0, 2.0, 5.0, 5.0, 5.0, 5.0, 5.0),
)
OUTAGE_POLICY = RetryPolicy(
    code="outage",
    max_attempts=12,
    delay_minutes_schedule=(
        5.0,
        15.0,
        30.0,
        60.0,
        60.0,
        60.0,
        60.0,
        60.0,
        60.0,
        60.0,
        60.0,
        60.0,
    ),
)
RETRY_AFTER_POLICY = RetryPolicy(
    code="retry_after",
    max_attempts=12,
    # schedule は server 指示で上書きされるので fallback として outage と同じ値
    delay_minutes_schedule=OUTAGE_POLICY.delay_minutes_schedule,
)
TIMEOUT_POLICY = RetryPolicy(
    code="timeout",
    max_attempts=8,
    delay_minutes_schedule=(2.0, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0),
)
UNKNOWN_POLICY = RetryPolicy(
    code="unknown",
    max_attempts=6,
    delay_minutes_schedule=(5.0, 15.0, 30.0, 60.0, 60.0, 60.0),
)


# delay の絶対上限 (分)。spec line 234 で 60 分 cap が指示されている。
MAX_DELAY_MINUTES = 60.0


def retry_policy_for(
    exc: TemporaryFetchError,
) -> tuple[RetryPolicy, float | None]:
    """exception 階層から policy を一意に決定する純関数。

    Returns:
        (policy, override_seconds):
          - ``override_seconds`` が ``None`` でなければ ``Retry-After`` の
            指示秒数。caller は policy.next_delay_minutes より優先して使う
            (後続 cap は ``MAX_DELAY_MINUTES`` を caller 側で適用)。
    """
    if isinstance(exc, ServerErrorRetryAfter):
        return RETRY_AFTER_POLICY, exc.retry_after_seconds
    if isinstance(exc, ServerErrorBlip):
        return BLIP_POLICY, None
    if isinstance(exc, ServerErrorOutage):
        return OUTAGE_POLICY, None
    if isinstance(exc, ReadTimeout):
        return TIMEOUT_POLICY, None
    # 素の TemporaryFetchError (未分類)
    return UNKNOWN_POLICY, None


def compute_next_delay_minutes(
    exc: TemporaryFetchError, attempt_count: int
) -> tuple[RetryPolicy, float]:
    """policy の選択 + delay 計算 + cap 適用までを一括する便利関数。

    ``Retry-After`` の上書きと ``MAX_DELAY_MINUTES`` cap 適用が
    Service 本体の散らかり要因になるので、両方を本関数で吸収する。
    """
    policy, override_seconds = retry_policy_for(exc)
    if override_seconds is not None:
        delay_minutes = override_seconds / 60.0
    else:
        delay_minutes = policy.next_delay_minutes(attempt_count)
    return policy, min(delay_minutes, MAX_DELAY_MINUTES)
