"""``FetchTools`` — 取得に使う共通道具箱 (P2-D)。

``XxxSource.collect(tools)`` が外部取得に使う stateless I/O クライアントだけを
束ねる純粋な道具箱。**新しいドメイン層ではない**: ``completion_profile`` や
``ObservedArticle`` 昇格判断は持たせない (それは Source の宣言 /
``passport_builder`` の責務であり、ここに混ぜると取得とドメイン判断が再び
癒着する)。共有 pipeline (multi-feed fan-out / Crossref item 変換等) も本型の
メソッドにはしない — ``tools`` を引数に取り ``FetchedArticle`` だけを yield する
free function として別モジュールに置き、Source がそれを選ぶ。

``ArticleFetcher`` が fetch 毎に既定構築する (旧 ``adapter_factory`` の
「fetch 毎に新 machinery」意味を保存)。test は本型 1 点に fake を注入する
(per-machinery コンストラクタ注入を 1 seam に集約 = ``tests/collection/
fetchers/_fixture_tools.py``)。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from app.collection.source_fetch.tools.algolia_hn_client import HackerNewsApiClient
from app.collection.source_fetch.tools.crossref_client import CrossrefApiClient
from app.collection.source_fetch.tools.raw_http_client import RawHttpClient
from app.collection.source_fetch.tools.rss_parser import RssParser


def _default_raw_http(accept: str) -> RawHttpClient:
    """既定の ``RawHttpClient`` factory (accept は呼び出し側=Source が選ぶ)。"""
    return RawHttpClient(accept=accept)


@dataclass(frozen=True, slots=True)
class FetchTools:
    """stateless I/O クライアントの束 (純粋な共通取得道具箱)。

    - ``rss`` / ``crossref`` / ``hacker_news``: no-arg 構築の共通クライアント。
    - ``raw_http_factory``: ``RawHttpClient`` は ``accept`` が呼び出し側依存
      (Anthropic=``application/xml`` / ORNL=``text/html`` の 2 者のみ) のため、
      collect 時に Source が ``accept`` を選べる factory にする。test は単一
      fake を ``accept`` 無視で返す factory を注入する。
    """

    rss: RssParser = field(default_factory=RssParser)
    crossref: CrossrefApiClient = field(default_factory=CrossrefApiClient)
    hacker_news: HackerNewsApiClient = field(default_factory=HackerNewsApiClient)
    raw_http_factory: Callable[[str], RawHttpClient] = field(
        default_factory=lambda: _default_raw_http
    )

    def raw_http(self, *, accept: str) -> RawHttpClient:
        """``accept`` 別の ``RawHttpClient`` を返す (sitemap/listing 取得用)。"""
        return self.raw_http_factory(accept)
