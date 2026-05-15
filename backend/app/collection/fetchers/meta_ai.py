"""Meta AI 用 Fetcher — about.fb.com から AI 関連のみ抽出。

per-source 設計:

- ENDPOINT: ``https://about.fb.com/news/feed/`` (Meta Newsroom)。``ai.meta.com``
  は専用 RSS / sitemap 一切提供なしのため代替経路として採用。
- RSS 2.0 + dc/content/media WordPress 標準。``<content:encoded>`` に full
  body (~3-4K chars) → Pattern R。
- **AI tag フィルタ必須**: Newsroom は WhatsApp / Threads / Sustainability 等
  全社カテゴリが流入する (実測 10 件中 6 件のみ AI tagged)。``<category>``
  集合に ``"AI"`` を含む entry のみ採用、それ以外は drop する (business critical)。
- attribution_label: ``"Meta Newsroom"``
"""

from __future__ import annotations

import html
import re
from collections.abc import AsyncIterator
from typing import ClassVar, Final

from app.collection.article.domain.article import ReadyForArticle
from app.collection.article.domain.value_objects import PublishedAt
from app.collection.fetchers.tools.rss_parser import RssEntry, RssParser
from app.shared.value_objects.canonical_article_url import CanonicalArticleUrl

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")

# AI 関連と判定する category 集合 (大文字小文字区別)。Meta Newsroom の
# `<category>` は "AI" / "Technology and Innovation" 等が混在するため、
# 厳密に "AI" tag を含むものだけを採用する (off-topic 取り込み防止)。
_AI_TAGS: Final[frozenset[str]] = frozenset({"AI"})


def _strip_html(s: str) -> str:
    """HTML タグを剥がして plain text に正規化する (body 用)。"""
    if not s:
        return ""
    return _WHITESPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", s))).strip()


def _pick_body(entry: RssEntry) -> str:
    """``content_encoded`` と ``summary`` の長い方を本文として採用。"""
    content_encoded = entry.content_encoded or ""
    summary = entry.summary or ""
    return content_encoded if len(content_encoded) >= len(summary) else summary


def _is_ai_tagged(tags: tuple[str, ...]) -> bool:
    """``tags`` に AI 判定 tag が含まれているか。"""
    return bool(_AI_TAGS.intersection(tags))


class MetaAIFetcher:
    """about.fb.com Newsroom から AI tagged entry のみを抽出する Pattern R Fetcher。

    AI フィルタ業務ロジックがクリティカル: Newsroom は全社混在で約 60% が
    非 AI 記事。spec の AI tag フィルタを構造的に絞り込み、off-topic 取り込み
    (=ニュース文脈ノイズ) を抑止する。
    """

    NAME: ClassVar[str] = "Meta AI"
    ENDPOINT_URL: ClassVar[str] = "https://about.fb.com/news/feed/"

    def __init__(self, parser: RssParser | None = None) -> None:
        self._parser = parser or RssParser()

    async def fetch(self, source_id: int) -> AsyncIterator[ReadyForArticle]:
        entries = await self._parser.fetch(
            endpoint_url=self.ENDPOINT_URL,
            source_name=self.NAME,
            parse_mode="bytes",
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
        # AI tag フィルタを最初に適用 (他フィールドの parse コストを節約)
        if not _is_ai_tagged(entry.tags):
            return None

        title = entry.title[:500]
        if not title:
            return None

        body = _strip_html(_pick_body(entry))
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
