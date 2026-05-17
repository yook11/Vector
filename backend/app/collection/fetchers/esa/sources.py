"""ESA Djangoplicity 系の具体 ``XxxSource`` (P2-D)。

ESA/Hubble + ESA/Webb は同型 (Djangoplicity News module) のため取得共通処理
``djangoplicity_entries`` (``esa/_common.py``) を共有する。継承はせず、各
Source が identity / 補完方針を ``ClassVar`` 宣言し ``collect`` で共通処理へ
委譲する (「ESA は Djangoplicity 規格 RSS」というソース固有の取得判断が本
クラスを見れば分かる)。将来 ESO / ALMA を追加する場合も本ファイルに Source を
1 件 + ``strategy.py`` / alembic に 1 行追加で済む。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import ClassVar

from app.collection.domain.observed_article import ObservedOrigin
from app.collection.domain.source_completion_profile import (
    DEFAULT_PROFILE,
    SourceCompletionProfile,
)
from app.collection.fetchers.esa._common import djangoplicity_entries
from app.collection.fetchers.tools.fetch_tools import FetchTools
from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.shared.value_objects.source_name import SourceName


class ESAHubbleSource:
    """ESA/Hubble news の Djangoplicity ``XxxSource`` (Pattern H)。"""

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
    """ESA/Webb news の Djangoplicity ``XxxSource`` (Pattern H)。"""

    name: ClassVar[SourceName] = SourceName("ESA/Webb")
    endpoint_url: ClassVar[str] = "https://esawebb.org/news/feed/"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_profile: ClassVar[SourceCompletionProfile] = DEFAULT_PROFILE

    @classmethod
    def collect(cls, tools: FetchTools) -> AsyncIterator[FetchedArticle]:
        return djangoplicity_entries(
            tools, source_name=str(cls.name), endpoint_url=cls.endpoint_url
        )
