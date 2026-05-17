"""ESA Djangoplicity 取得経路 (``DjangoplicityAdapter`` machinery) の単体テスト。

P2 で ESA/Hubble・ESA/Webb は継承具象を廃し、``DjangoplicityAdapter``
machinery に per-source config (``source_name`` / ``endpoint_url``) を注入する
形になった。固定する固有不変条件:

- Djangoplicity RSS は Pattern H のため ``collect()`` は ``body=None`` を
  yield し、``ArticleFetcher`` 経由で全 entry が ``ObservedArticle`` になる
  (identity = name/endpoint の固定は test_source_adapter_profiles に集約)
"""

from __future__ import annotations

from pathlib import Path

import feedparser
import pytest

from app.collection.domain.observed_article import ObservedArticle, ObservedOrigin
from app.collection.domain.source_completion_profile import DEFAULT_PROFILE
from app.collection.fetchers.article_fetcher import ArticleFetcher
from app.collection.fetchers.esa._common import DjangoplicityAdapter
from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.rss_parser import RssEntry, normalize_entry
from app.collection.sources.article_source import ArticleSource
from app.shared.value_objects.source_name import SourceName

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


def _adapter(
    *, source_name: str, endpoint_url: str, fixture: str
) -> DjangoplicityAdapter:
    return DjangoplicityAdapter(
        source_name=source_name,
        endpoint_url=endpoint_url,
        parser=_FakeRssParser(fixture),  # type: ignore[arg-type]
    )


def _source(adapter: DjangoplicityAdapter, *, name: str) -> ArticleSource:
    return ArticleSource(
        name=SourceName(name),
        endpoint_url="https://example.test/feed",
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: adapter,
    )


@pytest.mark.parametrize(
    ("source_name", "fixture", "endpoint_url"),
    [
        ("ESA/Hubble", "esa_hubble_rss.xml", "https://esahubble.org/news/feed/"),
        ("ESA/Webb", "esa_webb_rss.xml", "https://esawebb.org/news/feed/"),
    ],
)
async def test_pattern_h_yields_incomplete_only(
    source_name: str,
    fixture: str,
    endpoint_url: str,
) -> None:
    adapter = _adapter(
        source_name=source_name, endpoint_url=endpoint_url, fixture=fixture
    )
    collected: list[FetchedArticle] = [item async for item in adapter.collect()]
    assert collected
    assert all(item.body is None for item in collected)

    fetcher = ArticleFetcher(
        _source(
            _adapter(
                source_name=source_name, endpoint_url=endpoint_url, fixture=fixture
            ),
            name=source_name,
        )
    )
    passports = [p async for p in fetcher.fetch(source_id=1)]
    assert passports
    assert all(isinstance(p, ObservedArticle) for p in passports)
