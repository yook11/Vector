"""``FetchTools`` — Source が取得に使う stateless I/O クライアントの束。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from app.collection.source_fetch.tools.algolia_hn_client import HackerNewsApiClient
from app.collection.source_fetch.tools.crossref_client import CrossrefApiClient
from app.collection.source_fetch.tools.raw_http_client import RawHttpClient
from app.collection.source_fetch.tools.rss_parser import RssParser


def _default_raw_http(accept: str) -> RawHttpClient:
    """既定の ``RawHttpClient`` factory (accept は呼び出し側が選ぶ)。"""
    return RawHttpClient(accept=accept)


@dataclass(frozen=True, slots=True)
class FetchTools:
    """stateless I/O クライアントの束。"""

    rss: RssParser = field(default_factory=RssParser)
    crossref: CrossrefApiClient = field(default_factory=CrossrefApiClient)
    hacker_news: HackerNewsApiClient = field(default_factory=HackerNewsApiClient)
    raw_http_factory: Callable[[str], RawHttpClient] = field(
        default_factory=lambda: _default_raw_http
    )

    def raw_http(self, *, accept: str) -> RawHttpClient:
        """``accept`` 別の ``RawHttpClient`` を返す。"""
        return self.raw_http_factory(accept)
