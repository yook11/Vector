"""``FetchTools`` テスト seam (P2-D)。

P2-D で取得 machinery は ``XxxSource.collect(tools)`` になり、fake は
per-machinery コンストラクタ (`parser=`/`client=`) ではなく **``FetchTools``
1 点** に注入する。本モジュールはその単一注入ヘルパ。

- ``_FixtureRssReader``: ``RssReader`` の構造的 fake (fixture を feedparser で
  読み ``normalize_entry`` を通し本番と同じ ``RssEntry`` を返す)。本実装は P2
  までの各 test の ``_FixtureRssReader``/``_FakeRssReader`` と同一 (C2 で各
  test がこれへ repoint する)。
- ``fixture_tools``: 選択した fake を載せた ``FetchTools`` を構築する
  (未指定は実クライアント既定)。
"""

from __future__ import annotations

from pathlib import Path

import feedparser

from app.collection.article_collection.reader.rss_reader import (
    RssEntry,
    normalize_entry,
)
from app.collection.article_collection.tools.fetch_tools import FetchTools

_FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures"


class _FixtureRssReader:
    """``RssReader`` の構造的 fake。fixture を feedparser で読み、
    ``normalize_entry`` を通して本番経路と同じ ``RssEntry`` を返す。

    ``parse_mode`` / ``endpoint_url`` / ``source_name`` は受け取って無視する
    (fixture は静的バイナリなので encoding 差異を再現する必要がない)。本物の
    ``RssReader.fetch`` と同じ kw シグネチャを満たすため ``**_`` で耐える。
    """

    def __init__(self, fixture_filename: str) -> None:
        self._fixture_filename = fixture_filename

    async def fetch(
        self,
        *,
        endpoint_url: str,
        source_name: str,
        parse_mode: str = "text",
        **_: object,
    ) -> list[RssEntry]:
        path = _FIXTURES_DIR / self._fixture_filename
        feed = feedparser.parse(path.read_bytes())
        return [normalize_entry(raw) for raw in feed.entries]


def fixture_tools(
    *,
    rss_fixture: str | None = None,
    rss: object | None = None,
    crossref: object | None = None,
    hacker_news: object | None = None,
    raw: object | None = None,
) -> FetchTools:
    """選択した fake を載せた ``FetchTools`` を構築する。

    - ``rss``: ``RssReader`` 構造的 fake を直指定 (NASA fan-out 等の特殊 parser)。
    - ``rss_fixture``: 指定時 ``_FixtureRssReader(rss_fixture)`` を使う。
    - ``crossref`` / ``hacker_news`` / ``raw``: 各クライアントの構造的 fake。
      ``raw`` は ``accept`` を無視して同一 fake を返す factory として注入する。
    - 未指定の道具は実クライアント既定 (``FetchTools`` の default_factory)。
    """
    kwargs: dict[str, object] = {}
    if rss is not None:
        kwargs["rss"] = rss
    elif rss_fixture is not None:
        kwargs["rss"] = _FixtureRssReader(rss_fixture)
    if crossref is not None:
        kwargs["crossref"] = crossref
    if hacker_news is not None:
        kwargs["hacker_news"] = hacker_news
    if raw is not None:
        kwargs["raw_http_factory"] = lambda _accept: raw
    return FetchTools(**kwargs)  # type: ignore[arg-type]
