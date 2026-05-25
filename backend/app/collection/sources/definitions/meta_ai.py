"""Meta AI 用 Source — about.fb.com から AI 関連のみ抽出。

``ai.meta.com`` は専用 RSS / sitemap を提供しないため Meta Newsroom
(``https://about.fb.com/news/feed/``, RSS 2.0 WordPress) を代替経路として
採用する。Newsroom は WhatsApp / Threads / Sustainability 等の全社カテゴリが
混在する (実測 10 件中 6 件のみ AI tagged) ため ``<category>`` に ``"AI"`` を
含む entry のみ採用し他は対象外として除外する。``<content:encoded>`` に
full body を含む。attribution_label は ``"Meta Newsroom"``。
"""

from __future__ import annotations

import html
import re
from typing import ClassVar, Final

from app.collection.article_acquisition.fetched_article import FetchedArticle
from app.collection.article_acquisition.reader.rss_reader import RssEntry
from app.collection.article_acquisition.tools.reader_tools import ReaderTools
from app.collection.domain.observed_article import ObservedOrigin
from app.collection.sources.article_completion_policy import (
    DEFAULT_POLICY,
    ArticleCompletionPolicy,
)
from app.collection.sources.base_article_source import BaseArticleSource
from app.collection.sources.fetch_cadence import FetchCadence
from app.shared.value_objects.source_name import SourceName

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")

# AI 関連と判定する category 集合 (大文字小文字区別)。Meta Newsroom の
# `<category>` は "AI" / "Technology and Innovation" 等が混在するため、
# 厳密に "AI" tag を含むものだけを採用する (off-topic 取り込み防止)。
_AI_TAGS: Final[frozenset[str]] = frozenset({"AI"})


def _strip_html(s: str) -> str:
    if not s:
        return ""
    return _WHITESPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", s))).strip()


def _pick_body(entry: RssEntry) -> str:
    content_encoded = entry.content_encoded or ""
    summary = entry.summary or ""
    return content_encoded if len(content_encoded) >= len(summary) else summary


def is_collectable_meta_ai_entry(entry: RssEntry) -> bool:
    """Newsroom entry が AI tagged かを判定する public scope predicate。

    Meta Newsroom は WhatsApp / Threads / Sustainability 等の全社カテゴリが
    混在するため ``<category>`` に ``"AI"`` を含む entry のみを対象範囲として
    採用する。本判定は ``ConversionRejection`` でなく Source 層の意図的な
    収集スコープ宣言 (3rd 責務) — Reader が返した entry のうちどれを Source
    として収集対象とするかを named-public で表明する。
    """
    return bool(_AI_TAGS.intersection(entry.tags))


class MetaAISource(BaseArticleSource):
    """about.fb.com Newsroom から AI tagged entry のみ抽出する Source。

    AI tag フィルタで非 AI 記事 (約 60%) を対象外として除外する。
    """

    name: ClassVar[SourceName] = SourceName("Meta AI")
    endpoint_url: ClassVar[str] = "https://about.fb.com/news/feed/"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_policy: ClassVar[ArticleCompletionPolicy] = DEFAULT_POLICY
    fetch_cadence: ClassVar[FetchCadence] = FetchCadence.MEDIUM

    @classmethod
    async def read(cls, tools: ReaderTools) -> list[RssEntry]:
        return await tools.rss.fetch(
            endpoint_url=cls.endpoint_url,
            source_name=str(cls.name),
            parse_mode="bytes",
        )

    @classmethod
    def in_scope(cls, entry: RssEntry) -> bool:
        return is_collectable_meta_ai_entry(entry)

    @classmethod
    def map_entry(cls, entry: RssEntry) -> FetchedArticle:
        return FetchedArticle(
            title=entry.title,
            url=entry.link,
            body=_strip_html(_pick_body(entry)) or None,
            published_at=entry.published,
        )
