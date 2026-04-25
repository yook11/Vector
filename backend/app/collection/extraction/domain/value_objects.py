"""collection/extraction BC の値オブジェクト。

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
    """公開日時 VO — tzinfo=UTC を構造的に保証する。

    trafilatura が返す TZ なし日付文字列を UTC として解釈し、
    以降のドメイン/永続化レイヤで TZ 一貫性を保つ。
    """

    value: datetime

    def __post_init__(self) -> None:
        if self.value.tzinfo is None:
            raise ValueError("PublishedAt.value must be timezone-aware")

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
