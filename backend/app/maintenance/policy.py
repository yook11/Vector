"""back-fill 対象の年齢ウィンドウ (Policy)。

メインフローのリトライ猶予 (``pipeline_grace``) を待ってから back-fill 対象に
する。古すぎる記事 (``freshness_window`` 超え) はビジネス価値が薄いため対象外。

決定の一覧は ``plans/drafts/20260426-100205/PLAN.md`` §3-5 を参照。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


def utc_now() -> datetime:
    """UTC の現在時刻を返す (テスト時に freezegun 等で差し替えられる窓口)。"""
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class BackfillWindow:
    """back-fill 対象とする Article 年齢の境界。

    ``pipeline_grace``: メインフロー (chain) がリトライを終えるまでの猶予。
    これより新しい行はメインの完走を待つ。
    ``freshness_window``: ビジネス価値が残っている記事の上限年齢。
    これより古い行は救済対象から外す。
    """

    pipeline_grace: timedelta = timedelta(minutes=30)
    freshness_window: timedelta = timedelta(days=7)

    def boundaries_at(self, now: datetime) -> tuple[datetime, datetime]:
        """``now`` を基準とした (``created_before``, ``created_after``) を返す。

        ``created_before``: これより古い行のみが対象 (猶予経過)。
        ``created_after``: これより新しい行のみが対象 (鮮度維持)。
        """
        return (now - self.pipeline_grace, now - self.freshness_window)
