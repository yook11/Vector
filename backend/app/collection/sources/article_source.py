"""``ArticleSource`` — ニュースソースを 1 クラスで表す宣言的な構造的契約。

source は engine (``fetch_articles``) に駆動される **宣言** に徹する:

- ``read``: どの Reader を params 付きで呼ぶか (thin binding)
- ``in_scope``: 収集スコープ述語 (default: 全件採用)
- ``select``: dedup / order / limit (default: 恒等)
- ``map_entry``: Entry → ``FetchedArticle`` の写像 (total)

``in_scope`` / ``select`` の default は ``BaseArticleSource`` mixin が供給する
(Protocol の method body は非継承 class に生えないため)。registry は source を
instance 化せず class object のまま保持するので、属性は ``ClassVar``・メソッドは
``@classmethod``。
"""

from __future__ import annotations

from typing import ClassVar, Protocol, TypeVar, runtime_checkable

from app.collection.article_acquisition.fetched_article import FetchedArticle
from app.collection.article_acquisition.tools.reader_tools import ReaderTools
from app.collection.domain.observed_article import ObservedOrigin
from app.collection.sources.article_completion_policy import ArticleCompletionPolicy
from app.collection.sources.fetch_cadence import FetchCadence
from app.shared.value_objects.source_name import SourceName

# その source が使う Reader の Entry 型 (RssEntry / SitemapEntry / ...)。
T = TypeVar("T")


@runtime_checkable
class ArticleSource(Protocol[T]):
    """1 ニュースソース = identity + 補完方針 + 4 つの取得宣言。

    - ``name`` / ``endpoint_url``: ソース identity
    - ``observed_origin``: 取得チャネル (audit 用)
    - ``completion_policy``: 補完方針
    - ``fetch_cadence``: 取得間隔 tier (dispatch が cron に写像)
    """

    name: ClassVar[SourceName]
    endpoint_url: ClassVar[str]
    observed_origin: ClassVar[ObservedOrigin]
    completion_policy: ClassVar[ArticleCompletionPolicy]
    fetch_cadence: ClassVar[FetchCadence]

    @classmethod
    async def read(cls, tools: ReaderTools) -> list[T]:
        """どの Reader を params 付きで呼ぶか (thin binding)。"""
        ...

    @classmethod
    def in_scope(cls, entry: T) -> bool:
        """収集スコープ述語 (default: 全件採用)。"""
        ...

    @classmethod
    def select(cls, entries: list[T]) -> list[T]:
        """採用する entry 列を最終決定する純粋処理 = dedup / order / limit。"""
        ...

    @classmethod
    def map_entry(cls, entry: T) -> FetchedArticle:
        """Entry → ``FetchedArticle`` の写像 (total)。"""
        ...
