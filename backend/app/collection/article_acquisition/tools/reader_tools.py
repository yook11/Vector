"""``ReaderTools`` — Source が取得に使う stateless I/O クライアントの束。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from app.collection.article_acquisition.reader.algolia_hn_reader import HackerNewsReader
from app.collection.article_acquisition.reader.crossref_reader import CrossrefReader
from app.collection.article_acquisition.reader.html_listing_reader import (
    HtmlListingReader,
)
from app.collection.article_acquisition.reader.multi_feed_rss_reader import (
    MultiFeedRssReader,
)
from app.collection.article_acquisition.reader.rss_reader import RssReader
from app.collection.article_acquisition.reader.sitemap_reader import SitemapReader
from app.collection.article_acquisition.tools.raw_http_client import RawHttpClient
from app.config import settings


def _default_raw_http(accept: str) -> RawHttpClient:
    """既定の ``RawHttpClient`` factory (accept は呼び出し側が選ぶ)。"""
    return RawHttpClient(accept=accept)


def _default_crossref() -> CrossrefReader:
    """設定層の連絡先を注入した既定の Crossref reader を返す。"""
    return CrossrefReader(contact_email=str(settings.crossref_contact_email))


@dataclass(frozen=True, slots=True)
class ReaderTools:
    """stateless I/O クライアントの束。"""

    rss: RssReader = field(default_factory=RssReader)
    crossref: CrossrefReader = field(default_factory=_default_crossref)
    hacker_news: HackerNewsReader = field(default_factory=HackerNewsReader)
    raw_http_factory: Callable[[str], RawHttpClient] = field(
        default_factory=lambda: _default_raw_http
    )

    def raw_http(self, *, accept: str) -> RawHttpClient:
        """``accept`` 別の ``RawHttpClient`` を返す。"""
        return self.raw_http_factory(accept)

    def sitemap(self) -> SitemapReader:
        """sitemap Reader (transport は ``raw_http`` を wrap)。"""
        return SitemapReader(http=self.raw_http(accept="application/xml"))

    def html_listing(self) -> HtmlListingReader:
        """HTML listing Reader (transport は ``raw_http`` を wrap)。"""
        return HtmlListingReader(http=self.raw_http(accept="text/html"))

    def multi_feed_rss(self) -> MultiFeedRssReader:
        """複数 feed fan-out Reader (共有 ``rss`` を per-feed に駆動)。"""
        return MultiFeedRssReader(rss=self.rss)
