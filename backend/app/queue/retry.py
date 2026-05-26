"""taskiq retry 判定ヘルパー (タスクモジュール間で共有)。"""

from __future__ import annotations

from taskiq import Context


def is_last_attempt(ctx: Context) -> bool:
    """この試行後に SimpleRetryMiddleware がリトライしない場合 True を返す。"""
    labels = ctx.message.labels
    retry_count = int(labels.get("retry_count", 0))
    max_retries = int(labels.get("max_retries", 0))
    return retry_count >= max_retries
