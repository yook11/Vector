"""ESA Djangoplicity 取得経路 (``ESAHubbleSource`` / ``ESAWebbSource``)。

P2-D で ESA/Hubble・ESA/Webb は ``djangoplicity_entries`` 共通処理を共有する
独立 Source クラス (``esa.py``) になった。固定する固有不変条件:

- Djangoplicity RSS は Pattern H のため ``collect()`` は ``body=None`` を
  yield し、``ArticleFetcher`` 経由で全 entry が ``ObservedArticle`` になる
  (identity = name/endpoint の固定は test_source_adapter_profiles に集約)
"""

from __future__ import annotations

import pytest

from app.collection.domain.observed_article import ObservedArticle
from app.collection.source_fetch.article_fetcher import ArticleFetcher
from app.collection.source_fetch.fetched_article import FetchedArticle
from app.collection.sources.article_source import ArticleSource
from app.collection.sources.definitions.esa import (
    ESAHubbleSource,
    ESAWebbSource,
)
from tests.collection.fetchers._fixture_tools import fixture_tools


@pytest.mark.parametrize(
    ("source", "fixture"),
    [
        (ESAHubbleSource, "esa_hubble_rss.xml"),
        (ESAWebbSource, "esa_webb_rss.xml"),
    ],
)
async def test_pattern_h_yields_incomplete_only(
    source: ArticleSource,
    fixture: str,
) -> None:
    collected: list[FetchedArticle] = [
        item async for item in source.collect(fixture_tools(rss_fixture=fixture))
    ]
    assert collected
    assert all(item.body is None for item in collected)

    fetcher = ArticleFetcher(source, tools=fixture_tools(rss_fixture=fixture))
    passports = [p async for p in fetcher.fetch(source_id=1)]
    assert passports
    assert all(isinstance(p, ObservedArticle) for p in passports)
