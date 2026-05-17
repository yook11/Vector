"""MDPI journal の具体 ``XxxSource`` (P2-D)。

MDPI Materials / Energies / Sensors / Nanomaterials の 4 journal は同型
(Crossref API per-ISSN filter) のため取得共通処理 ``mdpi_items``
(``mdpi/_common.py``) を共有する。継承はせず、各 Source が identity / 補完方針
/ ISSN を ``ClassVar`` 宣言し ``collect`` で共通処理へ委譲する (「MDPI は
Crossref API 経路」というソース固有の取得判断が本クラスを見れば分かる)。
共有基底は作らず 4 ClassVar を各クラスに直書きする (継承で machinery を
共有しない方針を literal に遵守)。journal 識別は ``name`` に一本化。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import ClassVar

from app.collection.domain.observed_article import ObservedOrigin
from app.collection.domain.source_completion_profile import (
    DEFAULT_PROFILE,
    SourceCompletionProfile,
)
from app.collection.fetchers.mdpi._common import mdpi_items
from app.collection.fetchers.tools.fetch_tools import FetchTools
from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.shared.value_objects.source_name import SourceName

_MDPI_CROSSREF_ENDPOINT = "https://api.crossref.org/works"


class MDPIMaterialsSource:
    """MDPI Materials (ISSN 1996-1944, Pattern R)。"""

    name: ClassVar[SourceName] = SourceName("MDPI Materials")
    endpoint_url: ClassVar[str] = _MDPI_CROSSREF_ENDPOINT
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_profile: ClassVar[SourceCompletionProfile] = DEFAULT_PROFILE
    _ISSN: ClassVar[str] = "1996-1944"

    @classmethod
    def collect(cls, tools: FetchTools) -> AsyncIterator[FetchedArticle]:
        return mdpi_items(tools, source_name=str(cls.name), issn=cls._ISSN)


class MDPIEnergiesSource:
    """MDPI Energies (ISSN 1996-1073, Pattern R)。"""

    name: ClassVar[SourceName] = SourceName("MDPI Energies")
    endpoint_url: ClassVar[str] = _MDPI_CROSSREF_ENDPOINT
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_profile: ClassVar[SourceCompletionProfile] = DEFAULT_PROFILE
    _ISSN: ClassVar[str] = "1996-1073"

    @classmethod
    def collect(cls, tools: FetchTools) -> AsyncIterator[FetchedArticle]:
        return mdpi_items(tools, source_name=str(cls.name), issn=cls._ISSN)


class MDPISensorsSource:
    """MDPI Sensors (ISSN 1424-8220, Pattern R)。"""

    name: ClassVar[SourceName] = SourceName("MDPI Sensors")
    endpoint_url: ClassVar[str] = _MDPI_CROSSREF_ENDPOINT
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_profile: ClassVar[SourceCompletionProfile] = DEFAULT_PROFILE
    _ISSN: ClassVar[str] = "1424-8220"

    @classmethod
    def collect(cls, tools: FetchTools) -> AsyncIterator[FetchedArticle]:
        return mdpi_items(tools, source_name=str(cls.name), issn=cls._ISSN)


class MDPINanomaterialsSource:
    """MDPI Nanomaterials (ISSN 2079-4991, Pattern R)。"""

    name: ClassVar[SourceName] = SourceName("MDPI Nanomaterials")
    endpoint_url: ClassVar[str] = _MDPI_CROSSREF_ENDPOINT
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_profile: ClassVar[SourceCompletionProfile] = DEFAULT_PROFILE
    _ISSN: ClassVar[str] = "2079-4991"

    @classmethod
    def collect(cls, tools: FetchTools) -> AsyncIterator[FetchedArticle]:
        return mdpi_items(tools, source_name=str(cls.name), issn=cls._ISSN)
