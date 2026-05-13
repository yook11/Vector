"""RSS / Atom / RDF 経路の Fetcher 共通不変条件テスト。

各 Fetcher の per-source 実装ごとに以下の不変条件を検証する:

- 実 fixture から少なくとも 1 件は永続化 passport を yield する
- yield された passport は永続化不変条件 (5 fields strict) を満たす
- ``Fetcher.PROVIDES`` に列挙された key は全 entry の metadata に必ず含まれる
- metadata は ``pipeline_events.payload`` (JSONB) に焼ける primitive 構造のみ

ソース固有の挙動 (encoding 処理、独自フィルタ等) は各 Fetcher 単体テストで補う。
本テストは「pipeline 進行可能性」のゲートに集中する。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import feedparser
import pytest

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
from app.collection.ingestion.domain.fetched_article import FetchOutcome
from tests.collection.fetchers._invariant import (
    assert_at_least_one_passport,
    assert_metadata_audit_safe,
    assert_passports_persistable,
    assert_provides_contract,
)

_FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures"


def _convert_with_lang(
    fetcher: Any, entries: list[dict[str, Any]], lang: str
) -> list[FetchOutcome]:
    return [fetcher._convert_entry(e, 1, lang) for e in entries]


def _convert_no_lang(
    fetcher: Any, entries: list[dict[str, Any]], _lang: str
) -> list[FetchOutcome]:
    return [fetcher._convert_entry(e, 1) for e in entries]


# (fetcher_class, fixture_filename, feed_language, converter)
_CASES: list[tuple[type[Fetcher], str, str, Callable[..., list[FetchOutcome]]]] = [
    (CleanTechnicaFetcher, "cleantechnica_rss.xml", "en", _convert_with_lang),
    (CloudflareBlogFetcher, "cloudflare_rss.xml", "en", _convert_with_lang),
    (CornellChronicleFetcher, "cornell_rss.xml", "en", _convert_with_lang),
    (DeepMindFetcher, "deepmind_rss.xml", "en", _convert_with_lang),
    (EETimesJapanFetcher, "eetimes_japan_rss.xml", "ja", _convert_with_lang),
    (ElectrekFetcher, "electrek_rss.xml", "en", _convert_with_lang),
    (ELifeFetcher, "elife_rss.xml", "en", _convert_with_lang),
    (EngadgetFetcher, "engadget_rss.xml", "en", _convert_with_lang),
    (FierceBiotechFetcher, "fierce_biotech_rss.xml", "en", _convert_with_lang),
    (HuggingFaceBlogFetcher, "huggingface_blog_rss.xml", "en", _convert_with_lang),
    (IEEESpectrumFetcher, "ieee_spectrum_rss.xml", "en", _convert_with_lang),
    (ITmediaAIFetcher, "itmedia_ai_rss.xml", "ja", _convert_with_lang),
    (ITmediaNewsFetcher, "itmedia_news_rss.xml", "ja", _convert_with_lang),
    (JPCERTFetcher, "jpcert_rss.xml", "ja", _convert_with_lang),
    (KrebsOnSecurityFetcher, "krebs_on_security_rss.xml", "en", _convert_with_lang),
    (MetaAIFetcher, "meta_ai_rss.xml", "en", _convert_with_lang),
    (METIFetcher, "meti_atom.xml", "ja", _convert_with_lang),
    (MEXTFetcher, "mext_rdf.xml", "ja", _convert_with_lang),
    (MICFetcher, "mic_rdf.xml", "ja", _convert_with_lang),
    (MicrosoftResearchFetcher, "microsoft_research_rss.xml", "en", _convert_with_lang),
    (MONOistFetcher, "monoist_rss.xml", "ja", _convert_with_lang),
    (NASAFetcher, "nasa_rss.xml", "en-US", _convert_no_lang),
    (NISTFetcher, "nist_rss.xml", "en", _convert_with_lang),
    (NSFFetcher, "nsf_rss.xml", "en", _convert_with_lang),
    (OpenAIFetcher, "openai_rss.xml", "en", _convert_with_lang),
    (PLOSOneFetcher, "plos_one_atom.xml", "en", _convert_with_lang),
    (QuantumInsiderFetcher, "quantum_insider_rss.xml", "en", _convert_with_lang),
    (SpaceflightNowFetcher, "spaceflight_now_rss.xml", "en", _convert_no_lang),
    (SpaceNewsFetcher, "spacenews_rss.xml", "en", _convert_with_lang),
    (TechCrunchFetcher, "techcrunch_rss.xml", "en", _convert_with_lang),
    (TheRegisterFetcher, "the_register_atom.xml", "en", _convert_with_lang),
    (VentureBeatFetcher, "venturebeat_rss.xml", "en-US", _convert_with_lang),
]


_OutcomesAndClass = tuple[list[FetchOutcome], type[Fetcher]]


@pytest.fixture(params=_CASES, ids=lambda c: c[0].__name__)
def outcomes(request: pytest.FixtureRequest) -> _OutcomesAndClass:
    cls, fixture_name, lang, convert = request.param
    feed = feedparser.parse((_FIXTURES_DIR / fixture_name).read_bytes())
    fetcher = cls()
    return convert(fetcher, list(feed.entries), lang), cls


def test_fixture_yields_at_least_one_passport(
    outcomes: _OutcomesAndClass,
) -> None:
    assert_at_least_one_passport(outcomes[0])


def test_passports_satisfy_persistence_invariants(
    outcomes: _OutcomesAndClass,
) -> None:
    assert_passports_persistable(outcomes[0])


def test_provides_contract_holds(
    outcomes: _OutcomesAndClass,
) -> None:
    assert_provides_contract(outcomes[0], outcomes[1].PROVIDES)


def test_metadata_audit_safe(
    outcomes: _OutcomesAndClass,
) -> None:
    assert_metadata_audit_safe(outcomes[0])
