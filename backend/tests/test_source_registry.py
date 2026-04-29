"""ソースレジストリのテスト。"""

from unittest.mock import MagicMock

import pytest

from app.collection.domain.value_objects.source import SourceName
from app.collection.ingestion.registry import SourceFetcher, get_fetcher
from app.models.news_source import NewsSource


@pytest.mark.parametrize(
    "source_name",
    [
        "TechCrunch",
        "FierceBiotech",
        "The Quantum Insider",
        "IEEE Spectrum",
        "NASA",
        "Microsoft Research",
        "Krebs on Security",
        "VentureBeat",
        "Spaceflight Now",
        "ITmedia AI+",
        "JPCERT/CC",
        "Engadget",
        "CleanTechnica",
        "Electrek",
        "SpaceNews",
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
