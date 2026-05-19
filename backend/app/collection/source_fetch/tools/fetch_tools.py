"""``FetchTools`` — Source が取得に使う stateless I/O クライアントの束。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from app.collection.source_fetch.reader.algolia_hn_reader import HackerNewsReader
from app.collection.source_fetch.reader.crossref_reader import CrossrefReader
from app.collection.source_fetch.reader.html_listing_reader import HtmlListingReader
from app.collection.source_fetch.reader.rss_reader import RssReader
from app.collection.source_fetch.reader.sitemap_reader import SitemapReader
from app.collection.source_fetch.tools.raw_http_client import RawHttpClient


def _default_raw_http(accept: str) -> RawHttpClient:
    """既定の ``RawHttpClient`` factory (accept は呼び出し側が選ぶ)。"""
    return RawHttpClient(accept=accept)


@dataclass(frozen=True, slots=True)
class FetchTools:
    """stateless I/O クライアントの束。"""

    rss: RssReader = field(default_factory=RssReader)
    crossref: CrossrefReader = field(default_factory=CrossrefReader)
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
