"""FierceBiotech 用 Fetcher — Pattern H (RSS で URL 列挙、本文は HTML 必須)。

per-source 設計 (実 RSS 観察ベース):

- body は **読まない** (Pattern H、Stage 2 = HTML 抽出の責務)
- ``<pubDate>`` が **RFC822 非準拠** ("Apr 30, 2026 6:11pm") のため
  ``feedparser.published_parsed`` が落ちるケースを strptime fallback で救済。
  時刻部 TZ 情報なしのため ET (DST 自動切替) と仮定して UTC 換算する。
- language は ``feed.feed.language`` (= "en", NOT "en-US")。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import ClassVar
from zoneinfo import ZoneInfo

from app.collection.article.domain.value_objects import PublishedAt
from app.collection.fetchers.tools.rss_parser import RssEntry, RssParser
from app.collection.incomplete_article.domain.incomplete_article import (
    IncompleteArticle,
)
from app.shared.value_objects.canonical_article_url import CanonicalArticleUrl

_FB_PUBDATE_FORMAT = "%b %d, %Y %I:%M%p"
"""FierceBiotech 固有の pubDate format ("Apr 30, 2026 6:11pm")。

%b = 月名 3 文字 / %d = 日 / %Y = 4 桁年 / %I = 12 時間制 (非ゼロ埋め可) /
%M = 分 / %p = AM/PM (Linux glibc では am/pm/AM/PM すべて受理)。

実観察: "Apr 30, 2026 6:11pm" / "Apr 30, 2026 1:18pm" — 時刻部は非ゼロ埋め。
"""

_FB_TZ = ZoneInfo("America/New_York")
"""FierceBiotech の TZ 仮定 (Fierce Network = US biotech、東海岸)。

RSS には TZ 情報が含まれないため、ローカル発信時刻と推定して ET (DST 自動
切替) を適用する。本仮定は ±1 時間程度の誤差を許容する設計判断 (Stage 2 /
Stage 3 での ranking や digest week 算出に微影響あり)。
"""


def _parse_fb_published_at(raw: str | None) -> PublishedAt | None:
    """生 pubDate を ``%b %d, %Y %I:%M%p`` で解釈し ET TZ 付与後 UTC 変換。"""
    if not raw:
        return None
    try:
        dt = datetime.strptime(raw.strip(), _FB_PUBDATE_FORMAT)  # noqa: DTZ007
    except ValueError:
        return None
    return PublishedAt(value=dt.replace(tzinfo=_FB_TZ).astimezone(UTC))


class FierceBiotechFetcher:
    """FierceBiotech 用 Pattern H Fetcher。"""

    NAME: ClassVar[str] = "FierceBiotech"
    ENDPOINT_URL: ClassVar[str] = "https://www.fiercebiotech.com/rss/xml"

    def __init__(self, parser: RssParser | None = None) -> None:
        self._parser = parser or RssParser()

    async def fetch(self, source_id: int) -> AsyncIterator[IncompleteArticle]:
        entries = await self._parser.fetch(
            endpoint_url=self.ENDPOINT_URL,
            source_name=self.NAME,
            parse_mode="text",
        )
        for entry in entries:
            item = self._convert_entry(entry, source_id)
            if item is not None:
                yield item

    def _convert_entry(
        self,
        entry: RssEntry,
        source_id: int,
    ) -> IncompleteArticle | None:
        title = entry.title[:500]
        if not title:
            return None

        try:
            source_url = CanonicalArticleUrl(entry.link)
        except ValueError:
            return None

        # FB 固有: feedparser が non-RFC822 を解釈できない場合は strptime fallback。
        # Pattern H 固有: None でも drop しない (HTML 抽出で merge 後に確定)。
        if entry.published is not None:
            published_at_hint = PublishedAt(value=entry.published)
        else:
            published_at_hint = _parse_fb_published_at(
                entry.raw_published or entry.raw_updated
            )

        return IncompleteArticle(
            title=title,
            source_id=source_id,
            source_url=source_url,
            published_at_hint=published_at_hint,
        )
