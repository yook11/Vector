"""Microsoft Research 用 Source — Pattern R (RSS-only)。

RSS feed の ``<content:encoded>`` に full body (7000-65000 chars) を含むが
末尾に固定 footer ("Opens in a new tab The post {title} appeared first on
Microsoft Research.") がつくため、``_strip_html`` 後に per-source 定数
``_FOOTER_RE`` で除去する。

per-source 設計:

- body は ``entry.content_encoded`` 直取り **+ footer regex strip**
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
from app.shared.value_objects.source_name import SourceName

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")

# WordPress 由来の固定 footer。``_strip_html`` 後の plain text 末尾に付く。
# 全 entry で観察、``\s*`` で前後空白を吸収する。
_FOOTER_RE = re.compile(
    r"\s*Opens in a new tab\s*The post .* appeared first on Microsoft Research\.\s*$",
    re.DOTALL,
)


def _strip_html(s: str) -> str:
    """HTML タグを剥がして plain text に正規化する (body 用)。"""
    if not s:
        return ""
    return _WHITESPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", s))).strip()


def _strip_footer(body: str) -> str:
    """末尾の固定 boilerplate を per-source regex で除去する。"""
    return _FOOTER_RE.sub("", body)


class MicrosoftResearchSource:
    """Microsoft Research 用 ``XxxSource`` (Pattern R、body 信用)。

    body は ``_strip_html`` → ``_strip_footer`` の順で WordPress 固定 footer を
    除去してから渡す (builder では復元できない per-source 変換)。title /
    body 長 / published / URL の構造ゲートは ``fetched_article_converter`` に委譲する。
    """

    name: ClassVar[SourceName] = SourceName("Microsoft Research")
    endpoint_url: ClassVar[str] = "https://www.microsoft.com/en-us/research/feed/"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_profile: ClassVar[SourceCompletionProfile] = DEFAULT_PROFILE

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
