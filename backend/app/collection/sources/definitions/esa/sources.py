"""ESA Djangoplicity 系の Source 定義。"""

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
from app.collection.sources.definitions.esa._common import djangoplicity_entries
from app.shared.value_objects.source_name import SourceName


class ESAHubbleSource:
    """ESA/Hubble news (Djangoplicity RSS)。"""

    name: ClassVar[SourceName] = SourceName("ESA/Hubble")
    endpoint_url: ClassVar[str] = "https://esahubble.org/news/feed/"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_profile: ClassVar[SourceCompletionProfile] = DEFAULT_PROFILE

    @classmethod
    def collect(cls, tools: FetchTools) -> AsyncIterator[FetchedArticle]:
        return djangoplicity_entries(
            tools, source_name=str(cls.name), endpoint_url=cls.endpoint_url
        )


class ESAWebbSource:
    """ESA/Webb news (Djangoplicity RSS)。"""

    name: ClassVar[SourceName] = SourceName("ESA/Webb")
    endpoint_url: ClassVar[str] = "https://esawebb.org/news/feed/"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_profile: ClassVar[SourceCompletionProfile] = DEFAULT_PROFILE

    @classmethod
    def collect(cls, tools: FetchTools) -> AsyncIterator[FetchedArticle]:
        return djangoplicity_entries(
            tools, source_name=str(cls.name), endpoint_url=cls.endpoint_url
        )
