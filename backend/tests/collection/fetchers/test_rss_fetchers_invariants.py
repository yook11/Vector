"""RSS / Atom / RDF 経路の Fetcher 共通不変条件テスト。

各 Fetcher の per-source 実装ごとに以下の不変条件を検証する:

- 実 fixture から少なくとも 1 件は永続化 passport を yield する
- yield された passport は永続化不変条件 (5 fields strict) を満たす

ソース固有の挙動 (encoding 処理、独自フィルタ等) は各 Fetcher 単体テストで補う。
本テストは「pipeline 進行可能性」のゲートに集中する。
"""

from __future__ import annotations

from pathlib import Path

import feedparser
import pytest

from app.collection.article.domain.article import ReadyForArticle
from app.collection.fetchers.cleantechnica import CleanTechnicaFetcher
from app.collection.fetchers.cloudflare import CloudflareBlogFetcher
from app.collection.fetchers.cornell import CornellChronicleFetcher
from app.collection.fetchers.deepmind import DeepMindFetcher
from app.collection.fetchers.eetimes_japan import EETimesJapanFetcher
from app.collection.fetchers.electrek import ElectrekFetcher
from app.collection.fetchers.elife import ELifeFetcher
from app.collection.fetchers.engadget import EngadgetFetcher
from app.collection.fetchers.fierce_biotech import FierceBiotechFetcher
from app.collection.fetchers.huggingface import HuggingFaceBlogFetcher
from app.collection.fetchers.ieee_spectrum import IEEESpectrumFetcher
from app.collection.fetchers.itmedia_ai import ITmediaAIFetcher
from app.collection.fetchers.itmedia_news import ITmediaNewsFetcher
from app.collection.fetchers.jpcert import JPCERTFetcher
from app.collection.fetchers.krebs_on_security import KrebsOnSecurityFetcher
from app.collection.fetchers.meta_ai import MetaAIFetcher
from app.collection.fetchers.meti import METIFetcher
from app.collection.fetchers.mext import MEXTFetcher
from app.collection.fetchers.mic import MICFetcher
from app.collection.fetchers.microsoft_research import (
    MicrosoftResearchFetcher,
)
from app.collection.fetchers.monoist import MONOistFetcher
from app.collection.fetchers.nasa import NASAFetcher
from app.collection.fetchers.nist import NISTFetcher
from app.collection.fetchers.nsf import NSFFetcher
from app.collection.fetchers.openai import OpenAIFetcher
from app.collection.fetchers.plos_one import PLOSOneFetcher
from app.collection.fetchers.protocol import Fetcher
from app.collection.fetchers.quantum_insider import QuantumInsiderFetcher
from app.collection.fetchers.spaceflight_now import SpaceflightNowFetcher
from app.collection.fetchers.spacenews import SpaceNewsFetcher
from app.collection.fetchers.techcrunch import TechCrunchFetcher
from app.collection.fetchers.the_register import TheRegisterFetcher
from app.collection.fetchers.venturebeat import VentureBeatFetcher
from app.collection.incomplete_article.domain.incomplete_article import (
    IncompleteArticle,
)
from tests.collection.fetchers._invariant import (
    Passport,
    assert_at_least_one_passport,
    assert_passports_persistable,
)

_FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures"

# (fetcher_class, fixture_filename)
_CASES: list[tuple[type[Fetcher], str]] = [
    (CleanTechnicaFetcher, "cleantechnica_rss.xml"),
    (CloudflareBlogFetcher, "cloudflare_rss.xml"),
    (CornellChronicleFetcher, "cornell_rss.xml"),
    (DeepMindFetcher, "deepmind_rss.xml"),
    (EETimesJapanFetcher, "eetimes_japan_rss.xml"),
    (ElectrekFetcher, "electrek_rss.xml"),
    (ELifeFetcher, "elife_rss.xml"),
    (EngadgetFetcher, "engadget_rss.xml"),
    (FierceBiotechFetcher, "fierce_biotech_rss.xml"),
    (HuggingFaceBlogFetcher, "huggingface_blog_rss.xml"),
    (IEEESpectrumFetcher, "ieee_spectrum_rss.xml"),
    (ITmediaAIFetcher, "itmedia_ai_rss.xml"),
    (ITmediaNewsFetcher, "itmedia_news_rss.xml"),
    (JPCERTFetcher, "jpcert_rss.xml"),
    (KrebsOnSecurityFetcher, "krebs_on_security_rss.xml"),
    (MetaAIFetcher, "meta_ai_rss.xml"),
    (METIFetcher, "meti_atom.xml"),
    (MEXTFetcher, "mext_rdf.xml"),
    (MICFetcher, "mic_rdf.xml"),
    (MicrosoftResearchFetcher, "microsoft_research_rss.xml"),
    (MONOistFetcher, "monoist_rss.xml"),
    (NASAFetcher, "nasa_rss.xml"),
    (NISTFetcher, "nist_rss.xml"),
    (NSFFetcher, "nsf_rss.xml"),
    (OpenAIFetcher, "openai_rss.xml"),
    (PLOSOneFetcher, "plos_one_atom.xml"),
    (QuantumInsiderFetcher, "quantum_insider_rss.xml"),
    (SpaceflightNowFetcher, "spaceflight_now_rss.xml"),
    (SpaceNewsFetcher, "spacenews_rss.xml"),
    (TechCrunchFetcher, "techcrunch_rss.xml"),
    (TheRegisterFetcher, "the_register_atom.xml"),
    (VentureBeatFetcher, "venturebeat_rss.xml"),
]


@pytest.fixture(params=_CASES, ids=lambda c: c[0].__name__)
def passports(request: pytest.FixtureRequest) -> list[Passport]:
    cls, fixture_name = request.param
    feed = feedparser.parse((_FIXTURES_DIR / fixture_name).read_bytes())
    fetcher = cls()
    items: list[Passport] = []
    for entry in feed.entries:
        converted = fetcher._convert_entry(entry, 1)
        if converted is not None:
            assert isinstance(converted, ReadyForArticle | IncompleteArticle)
            items.append(converted)
    return items


def test_fixture_yields_at_least_one_passport(
    passports: list[Passport],
) -> None:
    assert_at_least_one_passport(passports)


def test_passports_satisfy_persistence_invariants(
    passports: list[Passport],
) -> None:
    assert_passports_persistable(passports)
