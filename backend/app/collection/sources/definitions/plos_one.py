"""PLOS ONE 用 Source (Atom 1.0)。

per-source 設計: Atom 1.0 仕様で ``<content type="html">`` に abstract 本文
(1.4K-3K chars 平均) を含む、Tier 1 ソース中で唯一の Atom feed。``content``
を一級採用、欠落時のみ ``summary`` (= Atom ``<summary>``) に fallback する。
"""

from __future__ import annotations

import html
import re
from collections.abc import AsyncIterator
from typing import ClassVar

from app.collection.domain.observed_article import ObservedOrigin
from app.collection.domain.source_completion_profile import (
    DEFAULT_PROFILE,
    SourceCompletionProfile,
)
from app.collection.source_fetch.fetched_article import FetchedArticle
from app.collection.source_fetch.tools.fetch_tools import FetchTools
from app.collection.source_fetch.tools.rss_parser import RssEntry
from app.shared.value_objects.source_name import SourceName

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _strip_html(s: str) -> str:
    if not s:
        return ""
    return _WHITESPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", s))).strip()


def _pick_body(entry: RssEntry) -> str:
    """Atom ``<content>`` を優先、欠落時のみ ``<summary>`` に fallback する。"""
    if entry.content_encoded:
        return entry.content_encoded
    return entry.summary or ""


class PLOSOneSource:
    """PLOS ONE 用 ``XxxSource`` (Atom 1.0、Pattern R、body 信用)。"""

    name: ClassVar[SourceName] = SourceName("PLOS ONE")
    endpoint_url: ClassVar[str] = "https://journals.plos.org/plosone/feed/atom"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_profile: ClassVar[SourceCompletionProfile] = DEFAULT_PROFILE

    @classmethod
    async def collect(cls, tools: FetchTools) -> AsyncIterator[FetchedArticle]:
        entries = await tools.rss.fetch(
            endpoint_url=cls.endpoint_url,
            source_name=str(cls.name),
            parse_mode="bytes",
        )
        for entry in entries:
            yield FetchedArticle(
                title=entry.title,
                url=entry.link,
                body=_strip_html(_pick_body(entry)) or None,
                published_at=entry.published,
            )
