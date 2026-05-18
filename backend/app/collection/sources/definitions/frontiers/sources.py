"""Frontiers Media journal の Source 定義。"""

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
from app.collection.sources.definitions.frontiers._common import frontiers_entries
from app.shared.value_objects.source_name import SourceName


class FrontiersAISource:
    """Frontiers in Artificial Intelligence。"""

    name: ClassVar[SourceName] = SourceName("Frontiers in Artificial Intelligence")
    endpoint_url: ClassVar[str] = (
        "https://www.frontiersin.org/journals/artificial-intelligence/rss"
    )
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_profile: ClassVar[SourceCompletionProfile] = DEFAULT_PROFILE

    @classmethod
    def collect(cls, tools: FetchTools) -> AsyncIterator[FetchedArticle]:
        return frontiers_entries(
            tools, source_name=str(cls.name), endpoint_url=cls.endpoint_url
        )


class FrontiersRoboticsAISource:
    """Frontiers in Robotics and AI。"""

    name: ClassVar[SourceName] = SourceName("Frontiers in Robotics and AI")
    endpoint_url: ClassVar[str] = (
        "https://www.frontiersin.org/journals/robotics-and-ai/rss"
    )
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_profile: ClassVar[SourceCompletionProfile] = DEFAULT_PROFILE

    @classmethod
    def collect(cls, tools: FetchTools) -> AsyncIterator[FetchedArticle]:
        return frontiers_entries(
            tools, source_name=str(cls.name), endpoint_url=cls.endpoint_url
        )


class FrontiersEnergyResearchSource:
    """Frontiers in Energy Research。"""

    name: ClassVar[SourceName] = SourceName("Frontiers in Energy Research")
    endpoint_url: ClassVar[str] = (
        "https://www.frontiersin.org/journals/energy-research/rss"
    )
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_profile: ClassVar[SourceCompletionProfile] = DEFAULT_PROFILE

    @classmethod
    def collect(cls, tools: FetchTools) -> AsyncIterator[FetchedArticle]:
        return frontiers_entries(
            tools, source_name=str(cls.name), endpoint_url=cls.endpoint_url
        )


class FrontiersMaterialsSource:
    """Frontiers in Materials。"""

    name: ClassVar[SourceName] = SourceName("Frontiers in Materials")
    endpoint_url: ClassVar[str] = "https://www.frontiersin.org/journals/materials/rss"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_profile: ClassVar[SourceCompletionProfile] = DEFAULT_PROFILE

    @classmethod
    def collect(cls, tools: FetchTools) -> AsyncIterator[FetchedArticle]:
        return frontiers_entries(
            tools, source_name=str(cls.name), endpoint_url=cls.endpoint_url
        )
