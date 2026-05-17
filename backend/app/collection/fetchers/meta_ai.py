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
from typing import Final

from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.rss_parser import RssEntry, RssParser

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


class MetaAIAdapter:
    """about.fb.com Newsroom から AI tagged entry のみ抽出する SourceAdapter。

    AI tag フィルタ (~60% drop) は business critical drop のため Adapter 内で
    旧 ``MetaAIFetcher._convert_entry`` と同位置 (最初) に適用する。title /
    body / published / URL の構造ゲートは ``passport_builder`` に委譲する。
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
            parse_mode="bytes",
        )
        for entry in entries:
            if not _is_ai_tagged(entry.tags):
                continue  # business critical drop (Newsroom 全社混在で約 60%)
            yield FetchedArticle(
                title=entry.title,
                url=entry.link,
                body=_strip_html(_pick_body(entry)) or None,
                published_at=entry.published,
            )
