"""Cornell Chronicle 用 Source (複数 feed)。

Cornell Chronicle (``https://news.cornell.edu/``) は学部別の
``/taxonomy/term/<id>/feed`` でカテゴリ別 RSS を提供する。本体 ``/news/feed``
は site-wide で雑多なため採用せず、対象 6 カテゴリのみを巡回する。feed は
Drupal 生成 RSS 2.0。description は短い概要のみで本文は HTML 取得に委ねる。
1 記事が複数 category に tag されるため feed 間で URL 重複が起きる
(横断 dedup は ``multi_feed_rss`` が担う)。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import ClassVar, Final

from app.collection.domain.observed_article import ObservedOrigin
from app.collection.source_fetch.fetched_article import FetchedArticle
from app.collection.source_fetch.tools.fetch_tools import FetchTools
from app.collection.source_fetch.tools.multi_feed_rss import multi_feed_rss
from app.collection.sources.article_completion_policy import (
    DEFAULT_POLICY,
    ArticleCompletionPolicy,
)
from app.shared.value_objects.source_name import SourceName

CORNELL_FEEDS: Final[tuple[str, ...]] = (
    # Artificial Intelligence
    "https://news.cornell.edu/taxonomy/term/24043/feed",
    # Computing & Information Sciences
    "https://news.cornell.edu/taxonomy/term/14256/feed",
    # Life Sciences & Veterinary Medicine
    "https://news.cornell.edu/taxonomy/term/15056/feed",
    # Energy, Environment & Sustainability
    "https://news.cornell.edu/taxonomy/term/15621/feed",
    # Physical Sciences & Engineering
    "https://news.cornell.edu/taxonomy/term/14252/feed",
    # Health, Nutrition & Medicine
    "https://news.cornell.edu/taxonomy/term/14248/feed",
)


class CornellChronicleSource:
    """Cornell Chronicle の複数 feed Source。"""

    name: ClassVar[SourceName] = SourceName("Cornell Chronicle")
    endpoint_url: ClassVar[str] = "https://news.cornell.edu/taxonomy/term/24043/feed"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_policy: ClassVar[ArticleCompletionPolicy] = DEFAULT_POLICY

    @classmethod
    def collect(cls, tools: FetchTools) -> AsyncIterator[FetchedArticle]:
        return multi_feed_rss(
            tools,
            source_name=str(cls.name),
            feeds=CORNELL_FEEDS,
            parse_mode="bytes",
        )
