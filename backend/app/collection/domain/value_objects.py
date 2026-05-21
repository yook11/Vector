"""collection BC の値オブジェクト。

- :class:`PublishedAt` — tzinfo=UTC を invariant として保持する公開日時 VO。

trafilatura は htmldate 経由で TZ なしの文字列を返すため、ここで UTC として
解釈して型に閉じ込める。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Self


@dataclass(frozen=True)
class PublishedAt:
    """公開日時 VO — tzinfo=UTC を不変条件として保持する。

    trafilatura が返す TZ なし日付文字列を UTC として解釈し、
    以降のレイヤで TZ 一貫性を保つ。
    """

    value: datetime

    def __post_init__(self) -> None:
        if self.value.tzinfo is None:
            raise ValueError("PublishedAt.value must be timezone-aware")

    @classmethod
    def from_datetime(cls, value: datetime | None) -> Self | None:
        """tz-aware なら VO、tz-naive / ``None`` なら ``None``。

        per-source が返す raw な ``datetime`` を不変条件 (tz-aware) に照らして
        採用可否に畳み込む factory。tz-naive を ValueError として再 raise せず
        不在として扱うのは「published 不在は獲得型不成立ではなく Observed で
        救う」という Stage 1 の責務階層に従うため。
        """
        if value is None or value.tzinfo is None:
            return None
        return cls(value=value)

    @classmethod
    def parse(cls, raw: str | None) -> Self | None:
        """trafilatura の日付文字列を解釈する。解釈不能なら ``None``。"""
        if not raw:
            return None
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return cls(datetime.strptime(raw, fmt).replace(tzinfo=UTC))
            except ValueError:
                continue
        return None
