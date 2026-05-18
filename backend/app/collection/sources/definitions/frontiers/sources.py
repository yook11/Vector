"""Frontiers Media journal の具体 ``XxxSource`` (P2-D)。

Frontiers in Artificial Intelligence / Robotics and AI / Energy Research /
Materials の 4 journal は同型 (Frontiers Media 標準 RSS) のため取得共通処理
``frontiers_entries`` (``frontiers/_common.py``) を共有する。継承はせず、各
Source が identity / 補完方針を ``ClassVar`` 宣言し ``collect`` で共通処理へ
委譲する。journal 識別は ``name`` に一本化。将来 journal を追加する場合も
本ファイルに Source を 1 件 + ``strategy.py`` / alembic に 1 行追加で済む。
"""

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
    """Frontiers in Artificial Intelligence (Pattern R)。"""

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
    """Frontiers in Robotics and AI (Pattern R)。"""

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
    """Frontiers in Energy Research (Pattern R)。"""

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
    """Frontiers in Materials (Pattern R)。"""

    name: ClassVar[SourceName] = SourceName("Frontiers in Materials")
    endpoint_url: ClassVar[str] = "https://www.frontiersin.org/journals/materials/rss"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_profile: ClassVar[SourceCompletionProfile] = DEFAULT_PROFILE

    @classmethod
    def collect(cls, tools: FetchTools) -> AsyncIterator[FetchedArticle]:
        return frontiers_entries(
            tools, source_name=str(cls.name), endpoint_url=cls.endpoint_url
        )
