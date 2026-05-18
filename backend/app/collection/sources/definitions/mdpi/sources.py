"""MDPI journal の Source 定義。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import ClassVar

from app.collection.domain.observed_article import ObservedOrigin
from app.collection.domain.source_completion_profile import (
    DEFAULT_PROFILE,
    SourceCompletionProfile,
)
from app.collection.source_fetch.fetched_article import FetchedArticle
from app.collection.source_fetch.tools.fetch_tools import FetchTools
from app.collection.sources.definitions.mdpi._common import mdpi_items
from app.shared.value_objects.source_name import SourceName

_MDPI_CROSSREF_ENDPOINT = "https://api.crossref.org/works"


class MDPIMaterialsSource:
    """MDPI Materials (ISSN 1996-1944)。"""

    name: ClassVar[SourceName] = SourceName("MDPI Materials")
    endpoint_url: ClassVar[str] = _MDPI_CROSSREF_ENDPOINT
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_profile: ClassVar[SourceCompletionProfile] = DEFAULT_PROFILE
    _ISSN: ClassVar[str] = "1996-1944"

    @classmethod
    def collect(cls, tools: FetchTools) -> AsyncIterator[FetchedArticle]:
        return mdpi_items(tools, source_name=str(cls.name), issn=cls._ISSN)


class MDPIEnergiesSource:
    """MDPI Energies (ISSN 1996-1073)。"""

    name: ClassVar[SourceName] = SourceName("MDPI Energies")
    endpoint_url: ClassVar[str] = _MDPI_CROSSREF_ENDPOINT
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_profile: ClassVar[SourceCompletionProfile] = DEFAULT_PROFILE
    _ISSN: ClassVar[str] = "1996-1073"

    @classmethod
    def collect(cls, tools: FetchTools) -> AsyncIterator[FetchedArticle]:
        return mdpi_items(tools, source_name=str(cls.name), issn=cls._ISSN)


class MDPISensorsSource:
    """MDPI Sensors (ISSN 1424-8220)。"""

    name: ClassVar[SourceName] = SourceName("MDPI Sensors")
    endpoint_url: ClassVar[str] = _MDPI_CROSSREF_ENDPOINT
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_profile: ClassVar[SourceCompletionProfile] = DEFAULT_PROFILE
    _ISSN: ClassVar[str] = "1424-8220"

    @classmethod
    def collect(cls, tools: FetchTools) -> AsyncIterator[FetchedArticle]:
        return mdpi_items(tools, source_name=str(cls.name), issn=cls._ISSN)


class MDPINanomaterialsSource:
    """MDPI Nanomaterials (ISSN 2079-4991)。"""

    name: ClassVar[SourceName] = SourceName("MDPI Nanomaterials")
    endpoint_url: ClassVar[str] = _MDPI_CROSSREF_ENDPOINT
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_profile: ClassVar[SourceCompletionProfile] = DEFAULT_PROFILE
    _ISSN: ClassVar[str] = "2079-4991"

    @classmethod
    def collect(cls, tools: FetchTools) -> AsyncIterator[FetchedArticle]:
        return mdpi_items(tools, source_name=str(cls.name), issn=cls._ISSN)
