"""``ArticleSource`` — ニュースソースを 1 クラスで表す構造的契約。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from app.collection.domain.observed_article import ObservedOrigin
from app.collection.domain.source_completion_profile import SourceCompletionProfile
from app.collection.source_fetch.fetched_article import FetchedArticle
from app.collection.source_fetch.tools.fetch_tools import FetchTools
from app.shared.value_objects.source_name import SourceName


@runtime_checkable
class ArticleSource(Protocol):
    """1 ニュースソース = identity + 補完方針 + 取得手順。

    - ``name`` / ``endpoint_url``: ソース identity
    - ``observed_origin``: 取得チャネル (audit 用)
    - ``completion_profile``: 補完方針
    - ``collect``: ``FetchTools`` で外部取得し ``FetchedArticle`` を yield
    """

    name: SourceName
    endpoint_url: str
    observed_origin: ObservedOrigin
    completion_profile: SourceCompletionProfile

    def collect(self, tools: FetchTools) -> AsyncIterator[FetchedArticle]: ...
