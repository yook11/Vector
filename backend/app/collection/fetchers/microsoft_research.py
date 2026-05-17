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

from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.rss_parser import RssParser

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


class MicrosoftResearchAdapter:
    """Microsoft Research 用 SourceAdapter (Pattern R、body 信用)。

    body は ``_strip_html`` → ``_strip_footer`` の順で WordPress 固定 footer を
    除去してから渡す (builder では復元できない per-source 変換)。title /
    body 長 / published / URL の構造ゲートは ``passport_builder`` に委譲する。
    """

    def __init__(
        self,
        *,
        endpoint_url: str,
        source_name: str,
        parser: RssParser | None = None,
    ) -> None:
        self._endpoint_url = endpoint_url
        self._source_name = source_name
        self._parser = parser or RssParser()

    async def collect(self) -> AsyncIterator[FetchedArticle]:
        entries = await self._parser.fetch(
            endpoint_url=self._endpoint_url,
            source_name=self._source_name,
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
