"""Cornell Chronicle — Pattern H / 複数 feed の ``XxxSource`` (P2-D)。

Cornell Chronicle (``https://news.cornell.edu/``) は学部別の
``/taxonomy/term/<id>/feed`` で AI / Computing / Life Sci / Energy / Phys Sci /
Health 等カテゴリ別 RSS を提供する。本体 ``/news/feed`` は site-wide 雑多な
ため採用せず、対象 6 カテゴリのみを巡回する。

P1 まで: 継承具象 (純 thin subclass)。
P2(B+C): 固有データを module-level config 化 (``CORNELL_FEEDS``)、
identity/補完方針は ``ArticleSource`` 集約が所有。
P2-D (本実装): Adapter 概念除去。``CornellChronicleSource`` が identity /
補完方針を ``ClassVar`` 宣言し ``collect`` で per-feed fan-out 共通処理
``multi_feed_rss`` へ委譲する (feed は Drupal 生成 RSS 2.0 =
``parse_mode="bytes"``、body builder は注入しない = Pattern H 既定。
description は短い概要のみで本文は HTML 取得に委譲)。

1 記事が複数 category に tag されるため feed 間 URL 重複が起きるが、feed 横断
dedup は ``multi_feed_rss`` 共通処理が担う。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import ClassVar, Final

from app.collection.domain.observed_article import ObservedOrigin
from app.collection.domain.source_completion_profile import (
    DEFAULT_PROFILE,
    SourceCompletionProfile,
)
from app.collection.source_fetch.fetched_article import FetchedArticle
from app.collection.source_fetch.tools.fetch_tools import FetchTools
from app.collection.source_fetch.tools.multi_feed_rss import multi_feed_rss
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
    """Cornell Chronicle の複数 feed ``XxxSource`` (Pattern H)。"""

    name: ClassVar[SourceName] = SourceName("Cornell Chronicle")
    endpoint_url: ClassVar[str] = "https://news.cornell.edu/taxonomy/term/24043/feed"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_profile: ClassVar[SourceCompletionProfile] = DEFAULT_PROFILE

    @classmethod
    def collect(cls, tools: FetchTools) -> AsyncIterator[FetchedArticle]:
        return multi_feed_rss(
            tools,
            source_name=str(cls.name),
            feeds=CORNELL_FEEDS,
            parse_mode="bytes",
        )
