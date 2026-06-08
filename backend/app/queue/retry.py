"""taskiq retry 判定ヘルパー (タスクモジュール間で共有)。"""

from __future__ import annotations

from taskiq import Context


def is_last_attempt(ctx: Context) -> bool:
    """この試行後に SimpleRetryMiddleware がリトライしない場合 True を返す。

    taskiq の SimpleRetryMiddleware は適用済みリトライ数を label ``_retries`` に
    持ち (実行中の値は 0..max_retries-1)、``_retries + 1 < max_retries`` の間だけ
    再投入する。よって「今が最後の試行」は ``_retries + 1 >= max_retries``。
    label 名・off-by-one とも middleware 実装に合わせる (``retry_count`` は
    middleware が書かない label で、参照すると常に give-up 判定が False になる)。
    """
    labels = ctx.message.labels
    retries = int(labels.get("_retries", 0))
    max_retries = int(labels.get("max_retries", 0))
    return retries + 1 >= max_retries
