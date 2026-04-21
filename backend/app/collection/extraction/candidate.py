"""抽出候補 VO — extraction 境界の中間表現と Repo ルックアップ結果型。

- :class:`PublishedAt` — tzinfo=UTC を invariant として保持する公開日時 VO。
- :class:`UnextractedDiscoveredArticle` — Article 未生成の DiscoveredArticle を表現。
- :data:`DiscoveredArticleLookup` — Repo が返すルックアップ結果 sum type。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Self

from app.domain.safe_url import SafeUrl


@dataclass(frozen=True)
class PublishedAt:
    """公開日時 VO — tzinfo=UTC を構造的に保証する。

    trafilatura は htmldate 経由で TZ なしの文字列を返すため、
    ここで UTC として解釈して型に閉じ込める。
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


@dataclass(frozen=True)
class UnextractedDiscoveredArticle:
    """Article が未生成の DiscoveredArticle。Repo が構築時点で保証する。"""

    id: int
    url: SafeUrl


@dataclass(frozen=True)
class UnextractedFound:
    """ルックアップ結果: 抽出対象の DiscoveredArticle が存在する。"""

    article: UnextractedDiscoveredArticle


@dataclass(frozen=True)
class AlreadyExtracted:
    """ルックアップ結果: 既に Article が存在する（冪等ヒット）。"""

    article_id: int


@dataclass(frozen=True)
class DiscoveredNotFound:
    """ルックアップ結果: DiscoveredArticle が存在しない。"""


DiscoveredArticleLookup = UnextractedFound | AlreadyExtracted | DiscoveredNotFound
