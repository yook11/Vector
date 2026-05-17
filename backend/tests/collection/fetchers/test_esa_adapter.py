"""ESA Adapter 群 (base + 2 thin subclass) の単体テスト (HTTP 非依存)。

固定する固有不変条件:

- Djangoplicity RSS は Pattern H のため ``collect()`` は ``body=None`` を
  yield し、``ArticleFetcher`` 経由で全 entry が ``ObservedArticle`` になる
- Hubble / Webb subclass の ``NAME`` / ``ENDPOINT_URL`` ClassVar が期待値と一致
"""

from __future__ import annotations

from pathlib import Path

import feedparser
import pytest

from app.collection.domain.observed_article import ObservedArticle
from app.collection.fetchers.article_fetcher import ArticleFetcher
from app.collection.fetchers.esa.hubble import ESAHubbleAdapter
from app.collection.fetchers.esa.webb import ESAWebbAdapter
from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.rss_parser import RssEntry, normalize_entry

_FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures"


class _FakeRssParser:
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


@pytest.mark.parametrize(
    ("adapter_cls", "fixture", "name", "endpoint_url"),
    [
        (
            ESAHubbleAdapter,
            "esa_hubble_rss.xml",
            "ESA/Hubble",
            "https://esahubble.org/news/feed/",
        ),
        (
            ESAWebbAdapter,
            "esa_webb_rss.xml",
            "ESA/Webb",
            "https://esawebb.org/news/feed/",
        ),
    ],
)
async def test_pattern_h_yields_incomplete_only(
    adapter_cls: type,
    fixture: str,
    name: str,
    endpoint_url: str,
) -> None:
    adapter = adapter_cls(parser=_FakeRssParser(fixture))

    collected: list[FetchedArticle] = [item async for item in adapter.collect()]
    assert collected
    assert all(item.body is None for item in collected)

    fetcher = ArticleFetcher(adapter_cls(parser=_FakeRssParser(fixture)))
    passports = [p async for p in fetcher.fetch(source_id=1)]
    assert passports
    assert all(isinstance(p, ObservedArticle) for p in passports)


@pytest.mark.parametrize(
    ("adapter_cls", "name", "endpoint_url"),
    [
        (ESAHubbleAdapter, "ESA/Hubble", "https://esahubble.org/news/feed/"),
        (ESAWebbAdapter, "ESA/Webb", "https://esawebb.org/news/feed/"),
    ],
)
def test_subclass_classvars(adapter_cls: type, name: str, endpoint_url: str) -> None:
    assert adapter_cls.NAME == name
    assert adapter_cls.ENDPOINT_URL == endpoint_url
