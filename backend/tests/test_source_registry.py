"""ソースレジストリのテスト。"""

from unittest.mock import MagicMock

import pytest

from app.collection.domain.value_objects.source import SourceName
from app.collection.ingestion.registry import SourceFetcher, get_fetcher
from app.models.news_source import NewsSource


@pytest.mark.parametrize(
    "source_name",
    [
        # NOTE: VentureBeat / TechCrunch (Phase 1a'/1b') と
        # The Quantum Insider / Krebs on Security / Spaceflight Now / NASA
        # (Phase 1c-A1) と IEEE Spectrum / Microsoft Research (Phase 1c-A2)
        # と ITmedia AI+ / ITmedia NEWS / MONOist / EE Times Japan / Engadget
        # (Phase 1c-C) と FierceBiotech (Phase 1c-D) は新 Protocol registry
        # (strategy.py) に移行済み (Pattern R 8 ソース + Pattern H 6 ソース完了)
        "JPCERT/CC",
        "CleanTechnica",
        "Electrek",
        "SpaceNews",
        "The Register",
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
