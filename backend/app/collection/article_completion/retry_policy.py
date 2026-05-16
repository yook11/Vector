"""エラー種別ごとの retry policy 純データ + 遅延算出モジュール。

Stage 2 disposition 設計の核: ``classify_external_fetch_error`` が origin
failure を ``Retryable`` に分類するとき、再投入の仕方を表す ``RetryPolicy``
を **データ** として載せる。``ArticleCompletionService`` は policy ごとに
コード分岐せず ``effective_delay_minutes`` で次回 ``ready_at`` の遅延を、
``policy.max_attempts`` で exhausted 判定だけを行う。

policy table の根拠は ``specs/pipeline-events-stage2-design.md`` line 226-244:

| エラー | delay schedule (分) | max attempts | 性質 |
|---|---|---|---|
| ConnectionError / 502 / 504 | 0.5 → 1 → 2 → 5 × 5 | 8 | blip-class |
| HTTP 503 (no Retry-After) | 5 → 15 → 30 → 60 × 9 | 12 | outage-class |
| HTTP 503 with Retry-After | header 値、後続 cap 60 分 | 12 | server-instructed |
| Read timeout | 2 → 5 × 7 | 8 | timeout (blip 寄り) |
| 未分類 origin failure | 5 → 15 → 30 → 60 × 3 | 6 | unknown (outage 寄り保守的) |

policy 値は spec の table を実装したスナップショット。運用観察後の
調整は **PR2.5-D** で行う (本 PR では table の値を変えない)。
"""

from __future__ import annotations

from dataclasses import dataclass


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


def effective_delay_minutes(
    policy: RetryPolicy,
    *,
    retry_after_seconds: float | None,
    attempt_count: int,
) -> float:
    """policy データだけで次回 retry までの遅延 (分) を算出する純関数。

    ``retry_after_seconds`` (server 指示) があれば分換算で優先、なければ
    ``policy.next_delay_minutes(attempt_count)``。どちらも ``MAX_DELAY_MINUTES``
    で cap する。exception 型に一切依存せず disposition が運ぶ ``RetryPolicy``
    と override 秒だけで完結する、Stage 2 disposition 経路の正準 API。
    """
    if retry_after_seconds is not None:
        delay = retry_after_seconds / 60.0
    else:
        delay = policy.next_delay_minutes(attempt_count)
    return min(delay, MAX_DELAY_MINUTES)
