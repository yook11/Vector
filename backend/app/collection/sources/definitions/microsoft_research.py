"""Microsoft Research 用 Source。

RSS feed の ``<content:encoded>`` に full body を含むが末尾に固定 footer
("Opens in a new tab The post {title} appeared first on Microsoft Research.")
がつくため ``_FOOTER_RE`` で除去する。
"""

from __future__ import annotations

import html
import re
from collections.abc import AsyncIterator
from typing import ClassVar

from app.collection.article_collection.fetched_article import FetchedArticle
from app.collection.article_collection.tools.fetch_tools import FetchTools
from app.collection.domain.observed_article import ObservedOrigin
from app.collection.sources.article_completion_policy import (
    DEFAULT_POLICY,
    ArticleCompletionPolicy,
)
from app.shared.value_objects.source_name import SourceName

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")

# WordPress 由来の固定 footer。全 entry の plain text 末尾に付く。
_FOOTER_RE = re.compile(
    r"\s*Opens in a new tab\s*The post .* appeared first on Microsoft Research\.\s*$",
    re.DOTALL,
)


def _strip_html(s: str) -> str:
    if not s:
        return ""
    return _WHITESPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", s))).strip()


def _strip_footer(body: str) -> str:
    """末尾の固定 footer を除去する。"""
    return _FOOTER_RE.sub("", body)


class MicrosoftResearchSource:
    """Microsoft Research 用 Source。"""

    name: ClassVar[SourceName] = SourceName("Microsoft Research")
    endpoint_url: ClassVar[str] = "https://www.microsoft.com/en-us/research/feed/"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_policy: ClassVar[ArticleCompletionPolicy] = DEFAULT_POLICY

    @classmethod
    async def collect(cls, tools: FetchTools) -> AsyncIterator[FetchedArticle]:
        entries = await tools.rss.fetch(
            endpoint_url=cls.endpoint_url,
            source_name=str(cls.name),
            parse_mode="text",
        )
        for entry in entries:
            body = _strip_footer(_strip_html(entry.content_encoded or ""))
            yield FetchedArticle(
                title=entry.title,
                url=entry.link,
                body=body or None,
                published_at=entry.published,
            )
