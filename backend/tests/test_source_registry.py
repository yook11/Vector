"""ソースレジストリのテスト。"""

from unittest.mock import MagicMock

import pytest

from app.collection.ingestion.registry import SourceFetcher, get_fetcher
from app.domain.news_source import SourceName
from app.models.news_source import NewsSource


def test_get_fetcher_returns_rss_fetcher() -> None:
    """RSS ソース名に対して RssFetcher が返る。"""
    source = MagicMock(spec=NewsSource)
    source.name = SourceName("TechCrunch")

    fetcher = get_fetcher(source)

    assert isinstance(fetcher, SourceFetcher)


def test_get_fetcher_returns_hn_fetcher() -> None:
    """Hacker News ソース名に対して HackerNewsFetcher が返る。"""
    source = MagicMock(spec=NewsSource)
    source.name = SourceName("Hacker News")

    fetcher = get_fetcher(source)

    assert isinstance(fetcher, SourceFetcher)


def test_get_fetcher_returns_av_fetcher() -> None:
    """Alpha Vantage ソース名に対して AlphaVantageFetcher が返る。"""
    source = MagicMock(spec=NewsSource)
    source.name = SourceName("Alpha Vantage")

    fetcher = get_fetcher(source)

    assert isinstance(fetcher, SourceFetcher)


def test_get_fetcher_raises_for_unknown_source() -> None:
    """未登録のソース名に対して KeyError が発生する。"""
    source = MagicMock(spec=NewsSource)
    source.name = SourceName("Unknown Source")

    with pytest.raises(KeyError):
        get_fetcher(source)


def test_rss_sources_share_same_fetcher() -> None:
    """RSS ソースは同一 RssFetcher インスタンスを共有する。"""
    source_a = MagicMock(spec=NewsSource)
    source_a.name = SourceName("TechCrunch")

    source_b = MagicMock(spec=NewsSource)
    source_b.name = SourceName("The Verge")

    fetcher_a = get_fetcher(source_a)
    fetcher_b = get_fetcher(source_b)

    assert fetcher_a is fetcher_b
