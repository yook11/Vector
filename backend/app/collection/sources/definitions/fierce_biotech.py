"""FierceBiotech 用 Source。

RSS で URL を列挙し本文は HTML 抽出に委ねる。``<pubDate>`` が RFC822 非準拠
("Apr 30, 2026 6:11pm") で ``feedparser.published_parsed`` が落ちる場合、
strptime fallback で救済する (TZ 情報が無いため ET = DST 自動切替と仮定して
UTC 換算)。language は ``feed.feed.language`` (= "en", NOT "en-US")。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar
from zoneinfo import ZoneInfo

from app.collection.article_acquisition.fetched_article import FetchedArticle
from app.collection.article_acquisition.reader.rss_reader import RssEntry
from app.collection.article_acquisition.tools.reader_tools import ReaderTools
from app.collection.domain.observed_article import ObservedOrigin
from app.collection.domain.value_objects import PublishedAt
from app.collection.sources.article_completion_policy import (
    DEFAULT_POLICY,
    ArticleCompletionPolicy,
)
from app.collection.sources.base_article_source import BaseArticleSource
from app.collection.sources.fetch_cadence import FetchCadence
from app.shared.value_objects.source_name import SourceName

_FB_PUBDATE_FORMAT = "%b %d, %Y %I:%M%p"
"""FierceBiotech 固有の pubDate format ("Apr 30, 2026 6:11pm")。

%b = 月名 3 文字 / %d = 日 / %Y = 4 桁年 / %I = 12 時間制 (非ゼロ埋め可) /
%M = 分 / %p = AM/PM (Linux glibc では am/pm/AM/PM すべて受理)。

実観察: "Apr 30, 2026 6:11pm" / "Apr 30, 2026 1:18pm" — 時刻部は非ゼロ埋め。
"""

_FB_TZ = ZoneInfo("America/New_York")
"""FierceBiotech の TZ 仮定 (US biotech、東海岸)。

RSS には TZ 情報が含まれないため ET (DST 自動切替) を適用する。±1 時間程度の
誤差を許容する。
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


class FierceBiotechSource(BaseArticleSource):
    """FierceBiotech 用 Source。

    ``<pubDate>`` が RFC822 非準拠で ``feedparser.published_parsed`` が落ちる
    場合のみ ``_parse_fb_published_at`` で strptime fallback (ET→UTC) を
    適用する。``published`` が ``None`` でも除外しない (HTML 抽出後に確定)。
    """

    name: ClassVar[SourceName] = SourceName("FierceBiotech")
    endpoint_url: ClassVar[str] = "https://www.fiercebiotech.com/rss/xml"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_policy: ClassVar[ArticleCompletionPolicy] = DEFAULT_POLICY
    fetch_cadence: ClassVar[FetchCadence] = FetchCadence.MEDIUM

    @classmethod
    async def read(cls, tools: ReaderTools) -> list[RssEntry]:
        return await tools.rss.fetch(
            endpoint_url=cls.endpoint_url,
            source_name=str(cls.name),
            parse_mode="text",
        )

    @classmethod
    def map_entry(cls, entry: RssEntry) -> FetchedArticle:
        published = entry.published
        if published is None:
            fb = _parse_fb_published_at(entry.raw_published or entry.raw_updated)
            published = fb.value if fb is not None else None
        return FetchedArticle(
            title=entry.title,
            url=entry.link,
            body=None,
            published_at=published,
        )
