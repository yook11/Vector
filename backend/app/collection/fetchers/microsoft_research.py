"""Microsoft Research 用 Fetcher — Pattern R (RSS-only)。

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

from app.collection.article.domain.article import ReadyForArticle
from app.collection.article.domain.value_objects import PublishedAt
from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.rss_parser import RssEntry, RssParser
from app.shared.value_objects.canonical_article_url import CanonicalArticleUrl

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


class MicrosoftResearchFetcher:
    """Microsoft Research 用 RSS-only Fetcher。

    footer は per-source 定数 ``_FOOTER_RE`` で除去する (regex match しなければ
    no-op、boilerplate がそのまま残るのは Stage 2 LLM 吸収範囲、logfire で検知)。
    """

    NAME: ClassVar[str] = "Microsoft Research"
    ENDPOINT_URL: ClassVar[str] = "https://www.microsoft.com/en-us/research/feed/"

    def __init__(self, parser: RssParser | None = None) -> None:
        self._parser = parser or RssParser()

    async def fetch(self, source_id: int) -> AsyncIterator[ReadyForArticle]:
        entries = await self._parser.fetch(
            endpoint_url=self.ENDPOINT_URL,
            source_name=self.NAME,
            parse_mode="text",
        )
        for entry in entries:
            item = self._convert_entry(entry, source_id)
            if item is not None:
                yield item

    def _convert_entry(
        self,
        entry: RssEntry,
        source_id: int,
    ) -> ReadyForArticle | None:
        title = entry.title[:500]
        if not title:
            return None

        # body: HTML strip → footer strip の順 (footer は plain text 末尾)
        body = _strip_footer(_strip_html(entry.content_encoded or ""))
        if len(body) < 50:
            return None

        if entry.published is None:
            return None

        try:
            source_url = CanonicalArticleUrl(entry.link)
        except ValueError:
            return None

        try:
            return ReadyForArticle(
                title=title,
                body=body,
                published_at=PublishedAt(value=entry.published),
                source_id=source_id,
                source_url=source_url,
            )
        except ValueError:
            return None


class MicrosoftResearchAdapter:
    """Microsoft Research 用 SourceAdapter (Pattern R、body 信用)。

    body は ``_strip_html`` → ``_strip_footer`` の順で WordPress 固定 footer を
    除去してから渡す (builder では復元できない per-source 変換)。title /
    body 長 / published / URL の構造ゲートは ``passport_builder`` に委譲する。
    """

    NAME = "Microsoft Research"
    ENDPOINT_URL = "https://www.microsoft.com/en-us/research/feed/"

    def __init__(self, parser: RssParser | None = None) -> None:
        self._parser = parser or RssParser()

    async def collect(self) -> AsyncIterator[FetchedArticle]:
        entries = await self._parser.fetch(
            endpoint_url=self.ENDPOINT_URL,
            source_name=self.NAME,
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
