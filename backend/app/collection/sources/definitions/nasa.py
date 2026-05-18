"""NASA — Pattern R / 複数 feed の ``XxxSource`` (P2-D)。

P1 まで: 継承具象が per-source 定数 (``FEEDS``) と Pattern R 拡張点 (本文
override) を保持。
P2(B+C): 固有データを module-level config 化 (``NASA_FEEDS`` /
``nasa_build_body``)、identity/補完方針は ``ArticleSource`` 集約が所有。
P2-D (本実装): Adapter 概念除去。``NASASource`` クラスが identity / 補完方針を
``ClassVar`` 宣言し ``collect`` で per-feed fan-out 共通処理 ``multi_feed_rss``
へ委譲する。「NASA は multi-feed RSS / 本文は ``content:encoded``」という
ソース固有の取得判断が本クラスを見れば分かる。

- ``NASA_FEEDS``: 6 feed (本体 + news-release / technology / aeronautics /
  station / artemis)。
- ``nasa_build_body``: body は ``entry.content_encoded`` (``<content:encoded>``)
  を ``_strip_html`` で plain text 化して直取り (nav noise 含むまま、Stage 2
  LLM 側で吸収する設計 = Pattern R)。

per-feed 失敗隔離・feed 横断 dedup・全 feed 失敗時 surface は
``multi_feed_rss`` 共通処理が一括で担う。``collect`` は async generator を
plain ``@classmethod`` が forward する (余分な frame を挟まず GeneratorExit /
re-raise 意味論を保存)。
"""

from __future__ import annotations

import html
import re
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
from app.collection.source_fetch.tools.rss_parser import RssEntry
from app.shared.value_objects.source_name import SourceName

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")

NASA_FEEDS: Final[tuple[str, ...]] = (
    "https://www.nasa.gov/feed/",
    "https://www.nasa.gov/news-release/feed/",
    "https://www.nasa.gov/technology/feed/",
    "https://www.nasa.gov/aeronautics/feed/",
    "https://www.nasa.gov/missions/station/feed/",
    "https://www.nasa.gov/missions/artemis/feed/",
)


def _strip_html(s: str) -> str:
    """HTML タグを剥がして plain text に正規化する (body 用)。"""
    if not s:
        return ""
    return _WHITESPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", s))).strip()


def nasa_build_body(entry: RssEntry) -> str | None:
    """Pattern R: ``content_encoded`` を plain text 化して本文に採用する。"""
    return _strip_html(entry.content_encoded or "") or None


class NASASource:
    """NASA news の複数 feed ``XxxSource`` (Pattern R)。"""

    name: ClassVar[SourceName] = SourceName("NASA")
    endpoint_url: ClassVar[str] = "https://www.nasa.gov/feed/"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_profile: ClassVar[SourceCompletionProfile] = DEFAULT_PROFILE

    @classmethod
    def collect(cls, tools: FetchTools) -> AsyncIterator[FetchedArticle]:
        return multi_feed_rss(
            tools,
            source_name=str(cls.name),
            feeds=NASA_FEEDS,
            parse_mode="text",
            body_builder=nasa_build_body,
        )
