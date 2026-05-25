"""MDPI journal の Crossref API 経路 (機構 + Source 定義)。

MDPI の RSS は Cloudflare WAF で 4 ISSN 全 403 となり使えないため Crossref
API の per-ISSN filter 経路を採る。``ISSN`` は Crossref filter に必須のため
Source が宣言し引数で渡す。``from-pub-date`` の ``lookback_days`` 窓は cron
周期と整合させ初回投入時の backfill を防ぐ。

収集スコープ (``is_collectable_mdpi_work``): MDPI が採るのは CC BY 4.0 の
journal-article のみ。corrections / editorials・非 CC BY・実体の薄い abstract・
日付不明は **ソースが意図的に採らない対象外データ** であって変換失敗でも
構造的非記事でもない (spec 第4責務 = 収集スコープ宣言。対象外を
``ConversionRejection`` 化も converter 移設もしない)。スコープ通過後の
degenerate (DOI 欠落 / 空 title) は写像で握りつぶさず素通しし converter が
``MISSING_URL`` / ``MISSING_TITLE`` として可視化する。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import ClassVar

from app.collection.article_acquisition.fetched_article import FetchedArticle
from app.collection.article_acquisition.reader.crossref_reader import CrossrefEntry
from app.collection.article_acquisition.tools.reader_tools import ReaderTools
from app.collection.domain.observed_article import ObservedOrigin
from app.collection.sources.article_completion_policy import (
    DEFAULT_POLICY,
    ArticleCompletionPolicy,
)
from app.collection.sources.base_article_source import BaseArticleSource
from app.collection.sources.fetch_cadence import FetchCadence
from app.shared.value_objects.source_name import SourceName

_MDPI_CROSSREF_ENDPOINT = "https://api.crossref.org/works"

_CC_BY_4_URL_RE = re.compile(r"creativecommons\.org/licenses/by/4\.0", re.IGNORECASE)
# 実体の薄い abstract は本文として信用しない閾値 (収集スコープ判定の一部)。
_MIN_BODY_LENGTH = 50


def _is_cc_by_4_0(license_urls: tuple[str, ...]) -> bool:
    """license URL のいずれかが CC BY 4.0 を指せば True。"""
    return any(_CC_BY_4_URL_RE.search(url) for url in license_urls)


def is_collectable_mdpi_work(entry: CrossrefEntry) -> bool:
    """MDPI が収集対象として宣言する work か (純粋なスコープ述語)。

    対象外 (非 journal-article / 非 CC BY 4.0 / 実体の薄い abstract / 日付
    不明) は変換失敗ではなく「ソースが意図的に採らない対象外データ」。
    """
    return (
        entry.entry_type == "journal-article"
        and _is_cc_by_4_0(entry.license_urls)
        and len(entry.body) >= _MIN_BODY_LENGTH
        and entry.published is not None
    )


def to_fetched_article(entry: CrossrefEntry) -> FetchedArticle:
    """in-scope な ``CrossrefEntry`` → ``FetchedArticle`` の純粋 total 写像。

    source_url は DOI の canonical resolver。DOI 欠落 / 空 title の degenerate
    は drop せず素通し、converter が可視化する (failure-visibility)。
    """
    return FetchedArticle(
        title=entry.title,
        url=f"https://doi.org/{entry.doi}" if entry.doi else "",
        body=entry.body,
        published_at=entry.published,
    )


async def mdpi_read(
    tools: ReaderTools,
    *,
    source_name: str,
    issn: str,
    lookback_days: int = 7,
    rows_per_request: int = 20,
) -> list[CrossrefEntry]:
    """MDPI journal の Crossref API 取得共通処理 (thin binding)。

    HTTP 取得 + parse + item→Entry 抽出は ``tools.crossref`` (Reader) に
    委譲する。``from-pub-date`` 窓だけ Source 宣言として組み立てる。
    """
    from_pub_date = (
        (datetime.now(UTC) - timedelta(days=lookback_days)).date().isoformat()
    )
    return await tools.crossref.fetch_works(
        source_name=source_name,
        issn=issn,
        from_pub_date=from_pub_date,
        rows=rows_per_request,
    )


class MDPIMaterialsSource(BaseArticleSource):
    """MDPI Materials (ISSN 1996-1944)。"""

    name: ClassVar[SourceName] = SourceName("MDPI Materials")
    endpoint_url: ClassVar[str] = _MDPI_CROSSREF_ENDPOINT
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_policy: ClassVar[ArticleCompletionPolicy] = DEFAULT_POLICY
    fetch_cadence: ClassVar[FetchCadence] = FetchCadence.LOW
    _ISSN: ClassVar[str] = "1996-1944"

    @classmethod
    async def read(cls, tools: ReaderTools) -> list[CrossrefEntry]:
        return await mdpi_read(tools, source_name=str(cls.name), issn=cls._ISSN)

    @classmethod
    def in_scope(cls, entry: CrossrefEntry) -> bool:
        return is_collectable_mdpi_work(entry)

    @classmethod
    def map_entry(cls, entry: CrossrefEntry) -> FetchedArticle:
        return to_fetched_article(entry)


class MDPIEnergiesSource(BaseArticleSource):
    """MDPI Energies (ISSN 1996-1073)。"""

    name: ClassVar[SourceName] = SourceName("MDPI Energies")
    endpoint_url: ClassVar[str] = _MDPI_CROSSREF_ENDPOINT
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_policy: ClassVar[ArticleCompletionPolicy] = DEFAULT_POLICY
    fetch_cadence: ClassVar[FetchCadence] = FetchCadence.LOW
    _ISSN: ClassVar[str] = "1996-1073"

    @classmethod
    async def read(cls, tools: ReaderTools) -> list[CrossrefEntry]:
        return await mdpi_read(tools, source_name=str(cls.name), issn=cls._ISSN)

    @classmethod
    def in_scope(cls, entry: CrossrefEntry) -> bool:
        return is_collectable_mdpi_work(entry)

    @classmethod
    def map_entry(cls, entry: CrossrefEntry) -> FetchedArticle:
        return to_fetched_article(entry)


class MDPISensorsSource(BaseArticleSource):
    """MDPI Sensors (ISSN 1424-8220)。"""

    name: ClassVar[SourceName] = SourceName("MDPI Sensors")
    endpoint_url: ClassVar[str] = _MDPI_CROSSREF_ENDPOINT
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_policy: ClassVar[ArticleCompletionPolicy] = DEFAULT_POLICY
    fetch_cadence: ClassVar[FetchCadence] = FetchCadence.LOW
    _ISSN: ClassVar[str] = "1424-8220"

    @classmethod
    async def read(cls, tools: ReaderTools) -> list[CrossrefEntry]:
        return await mdpi_read(tools, source_name=str(cls.name), issn=cls._ISSN)

    @classmethod
    def in_scope(cls, entry: CrossrefEntry) -> bool:
        return is_collectable_mdpi_work(entry)

    @classmethod
    def map_entry(cls, entry: CrossrefEntry) -> FetchedArticle:
        return to_fetched_article(entry)


class MDPINanomaterialsSource(BaseArticleSource):
    """MDPI Nanomaterials (ISSN 2079-4991)。"""

    name: ClassVar[SourceName] = SourceName("MDPI Nanomaterials")
    endpoint_url: ClassVar[str] = _MDPI_CROSSREF_ENDPOINT
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_policy: ClassVar[ArticleCompletionPolicy] = DEFAULT_POLICY
    fetch_cadence: ClassVar[FetchCadence] = FetchCadence.LOW
    _ISSN: ClassVar[str] = "2079-4991"

    @classmethod
    async def read(cls, tools: ReaderTools) -> list[CrossrefEntry]:
        return await mdpi_read(tools, source_name=str(cls.name), issn=cls._ISSN)

    @classmethod
    def in_scope(cls, entry: CrossrefEntry) -> bool:
        return is_collectable_mdpi_work(entry)

    @classmethod
    def map_entry(cls, entry: CrossrefEntry) -> FetchedArticle:
        return to_fetched_article(entry)
