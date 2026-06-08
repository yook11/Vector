"""``is_last_attempt`` の不変条件テスト (正本)。

taskiq SimpleRetryMiddleware の仕様:
- 適用済みリトライ数を label ``_retries`` に書く (実行中の値は 0..max_retries-1)
- ``_retries + 1 < max_retries`` の間だけ再投入する

よって「今が最後の試行」⇔ ``_retries + 1 >= max_retries``
一般則: 最終試行 ⇔ ``_retries == max_retries - 1``
       非最終試行は ``max_retries >= 2`` のときのみ存在する

真理値表 (期待値はこの仕様から導出、production ロジックの呼び出しで作っていない):
  max_retries=0, _retries=0  → True  (リトライ無効、唯一の試行が最後)
  max_retries=1, _retries=0  → True  (唯一の試行が最後)
  max_retries=2, _retries=0  → False (1回目、retry 余地あり)
  max_retries=2, _retries=1  → True  (2回目=最終試行)
  max_retries=3, _retries=0  → False
  max_retries=3, _retries=1  → False
  max_retries=3, _retries=2  → True  (最終試行)
  label 欠落               → True  (0 + 1 >= 0、label 未設定でも last 扱い)
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _make_ctx(labels: dict) -> MagicMock:
    """``ctx.message.labels`` を持つ最小 Context モック。"""
    ctx = MagicMock()
    ctx.message.labels = labels
    return ctx


# 真理値表の全ケースを parametrize で網羅する
# expected は上記仕様の真理値表から直接決定 (production 関数を呼んで作っていない)
@pytest.mark.parametrize(
    "max_retries, retries, expected",
    [
        # リトライ無効 (max_retries=0): 唯一の試行が最後
        (0, 0, True),
        # max_retries=1: 非最終試行は存在しない、0回目が唯一=最終
        (1, 0, True),
        # max_retries=2: 0回目は非最終、1回目が最終
        (2, 0, False),
        (2, 1, True),
        # max_retries=3: 0・1回目は非最終、2回目が最終
        (3, 0, False),
        (3, 1, False),
        (3, 2, True),
    ],
)
def test_is_last_attempt_truth_table(
    max_retries: int, retries: int, expected: bool
) -> None:
    """真理値表の各行を検証する。

    期待値は taskiq SimpleRetryMiddleware 仕様 (_retries + 1 >= max_retries) から
    導出しており、production 関数を呼んで導いていない。
    """
    from app.queue.retry import is_last_attempt

    ctx = _make_ctx({"_retries": retries, "max_retries": max_retries})
    assert is_last_attempt(ctx) is expected


def test_is_last_attempt_missing_labels_returns_true() -> None:
    """label が両方欠落した場合は True を返す。

    int(labels.get("_retries", 0)) + 1 >= int(labels.get("max_retries", 0))
    = 0 + 1 >= 0 = True。
    label を読めない構成 (middleware が書く前の初回など) でも last 扱いになる契約を
    pin する。
    """
    from app.queue.retry import is_last_attempt

    ctx = _make_ctx({})
    assert is_last_attempt(ctx) is True


def test_is_last_attempt_only_retries_missing_returns_correct() -> None:
    """``_retries`` だけ欠落 (max_retries のみある) は 0 として計算される。"""
    from app.queue.retry import is_last_attempt

    # max_retries=2, _retries=0 (欠落) → 0 + 1 >= 2 は False
    ctx = _make_ctx({"max_retries": 2})
    assert is_last_attempt(ctx) is False


def test_is_last_attempt_only_max_retries_missing_returns_true() -> None:
    """``max_retries`` だけ欠落 (_retries のみある) は 0 として計算される。

    _retries + 1 >= 0 は常に True。
    """
    from app.queue.retry import is_last_attempt

    ctx = _make_ctx({"_retries": 1})
    assert is_last_attempt(ctx) is True
