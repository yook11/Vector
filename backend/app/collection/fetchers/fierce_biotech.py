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
from zoneinfo import ZoneInfo

from app.collection.domain.observed_article import ObservedOrigin
from app.collection.domain.source_completion_profile import DEFAULT_PROFILE
from app.collection.domain.value_objects import PublishedAt
from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.rss_parser import RssParser

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


class FierceBiotechAdapter:
    """FierceBiotech 用 SourceAdapter (Pattern H)。

    ``<pubDate>`` が RFC822 非準拠 ("Apr 30, 2026 6:11pm") で
    ``feedparser.published_parsed`` が落ちる場合のみ ``_parse_fb_published_at``
    で strptime fallback (ET→UTC) を適用する (builder では復元できない
    per-source 変換)。Pattern H のため ``published`` が ``None`` でも drop
    しない (HTML 抽出後に merge 確定)。
    """

    NAME = "FierceBiotech"
    ENDPOINT_URL = "https://www.fiercebiotech.com/rss/xml"
    observed_origin = ObservedOrigin.feed
    completion_profile = DEFAULT_PROFILE

    def __init__(self, parser: RssParser | None = None) -> None:
        self._parser = parser or RssParser()

    async def collect(self) -> AsyncIterator[FetchedArticle]:
        entries = await self._parser.fetch(
            endpoint_url=self.ENDPOINT_URL,
            source_name=self.NAME,
            parse_mode="text",
        )
        for entry in entries:
            published = entry.published
            if published is None:
                fb = _parse_fb_published_at(entry.raw_published or entry.raw_updated)
                published = fb.value if fb is not None else None
            yield FetchedArticle(
                title=entry.title,
                url=entry.link,
                body=None,
                published_at=published,
            )
