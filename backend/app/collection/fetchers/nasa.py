"""NASA 用 Fetcher — Pattern R (RSS-only)、複数 feed 巡回 + URL dedup。

per-source 設計:

- body は ``entry.content_encoded`` (``<content:encoded>``) を**直取り**
  (nav noise 含むまま、Stage 2 LLM 側で吸収する設計)

複数 feed 巡回:

- 6 feed (本体 + news-release / technology / aeronautics / station / artemis)
  を ``FEEDS`` ClassVar で保持
- ``fetch()`` で順次 GET → 1 feed の ``TemporaryFetchError`` は warn して次
  feed に進む (全停止しない)。``PermanentFetchError`` は source 全体失敗として
  伝播する (catch しない)
- in-memory ``seen_urls: set[str]`` で同 cron 周期内の重複 URL を排除
"""

from __future__ import annotations

import html
import re
from collections.abc import AsyncIterator
from typing import ClassVar

import structlog

from app.collection.errors import TemporaryFetchError
from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.rss_parser import RssParser

logger = structlog.get_logger(__name__)

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _strip_html(s: str) -> str:
    """HTML タグを剥がして plain text に正規化する (body 用)。"""
    if not s:
        return ""
    return _WHITESPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", s))).strip()


class NASAAdapter:
    """NASA 用 SourceAdapter (Pattern R、6 feed 巡回 + URL dedup)。

    1 feed の ``TemporaryFetchError`` は ``nasa_feed_skip`` warning を残して
    次 feed に進む (旧 ``NASAFetcher`` と同挙動、運用可観測性維持)。
    ``PermanentFetchError`` は catch せず source 全体失敗として伝播する。
    cron 周期内の重複 URL は in-memory ``seen_urls`` で排除する。
    """

    NAME = "NASA"
    ENDPOINT_URL = "https://www.nasa.gov/feed/"
    FEEDS: ClassVar[tuple[str, ...]] = (
        "https://www.nasa.gov/feed/",
        "https://www.nasa.gov/news-release/feed/",
        "https://www.nasa.gov/technology/feed/",
        "https://www.nasa.gov/aeronautics/feed/",
        "https://www.nasa.gov/missions/station/feed/",
        "https://www.nasa.gov/missions/artemis/feed/",
    )

    def __init__(self, parser: RssParser | None = None) -> None:
        self._parser = parser or RssParser()

    async def collect(self) -> AsyncIterator[FetchedArticle]:
        seen_urls: set[str] = set()
        for feed_url in self.FEEDS:
            try:
                entries = await self._parser.fetch(
                    endpoint_url=feed_url,
                    source_name=self.NAME,
                    parse_mode="text",
                )
            except TemporaryFetchError as e:
                # 1 feed の transient 失敗で全停止しない (他 feed は続行)
                logger.warning(
                    "nasa_feed_skip",
                    source=self.NAME,
                    feed=feed_url,
                    error=str(e),
                )
                continue
            # PermanentFetchError は catch しない (source 全体失敗として伝播)
            for entry in entries:
                if entry.link and entry.link in seen_urls:
                    continue
                if entry.link:
                    seen_urls.add(entry.link)
                yield FetchedArticle(
                    title=entry.title,
                    url=entry.link,
                    body=_strip_html(entry.content_encoded or "") or None,
                    published_at=entry.published,
                )
