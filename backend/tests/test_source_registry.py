"""ソースレジストリのテスト。"""

from unittest.mock import MagicMock

import pytest

from app.collection.domain.value_objects.source import SourceName
from app.collection.ingestion.registry import SourceFetcher, get_fetcher
from app.models.news_source import NewsSource


@pytest.mark.parametrize(
    "source_name",
    [
        # NOTE: Phase 1d 完了時点で RSS 全 18 ソース (Pattern R 8/8 +
        # Pattern H 8/8 + Pattern R+H 4/4) は新 Protocol registry
        # (``strategy.NEW_ROUTE_FETCHERS``) に移行済み。本 registry には
        # API Pattern の Hacker News 1 ソースのみが残る。PR-1e (HN を
        # 新 Protocol 化) で本テストごと削除予定。
        "Hacker News",
    ],
)
def test_get_fetcher_returns_source_fetcher(source_name: str) -> None:
    """登録済みソース名に対して SourceFetcher が返る。"""
    source = MagicMock(spec=NewsSource)
    source.name = SourceName(source_name)

    fetcher = get_fetcher(source)

    assert isinstance(fetcher, SourceFetcher)


def test_get_fetcher_raises_for_unknown_source() -> None:
    """未登録のソース名に対して KeyError が発生する。"""
    source = MagicMock(spec=NewsSource)
    source.name = SourceName("Unknown Source")

    with pytest.raises(KeyError):
        get_fetcher(source)
