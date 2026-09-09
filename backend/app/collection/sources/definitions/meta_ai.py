"""Meta AIのRSS取得宣言。"""

from typing import ClassVar

from app.collection.article_acquisition.reader.rss_reader import RssEntry
from app.collection.domain.observed_article import ObservedOrigin
from app.collection.sources.article_completion_policy import (
    DEFAULT_POLICY,
    ArticleCompletionPolicy,
)
from app.collection.sources.fetch_cadence import FetchCadence
from app.collection.sources.rss_acquisition import RssAcquisition, RssBodyPolicy
from app.collection.sources.source_name import SourceName


def is_collectable_meta_ai_entry(entry: RssEntry) -> bool:
    """大文字小文字を区別してAIタグを含む候補だけを収集する。"""
    return "AI" in entry.tags


class MetaAISource:
    name: ClassVar[SourceName] = SourceName("Meta AI")
    acquisition: ClassVar[RssAcquisition] = RssAcquisition(
        feeds=("https://about.fb.com/news/feed/",),
        parse_mode="bytes",
        body_policy=RssBodyPolicy.LONGEST_CONTENT_OR_SUMMARY,
    )
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_policy: ClassVar[ArticleCompletionPolicy] = DEFAULT_POLICY
    fetch_cadence: ClassVar[FetchCadence] = FetchCadence.MEDIUM

    @staticmethod
    def in_scope(entry: RssEntry) -> bool:
        return is_collectable_meta_ai_entry(entry)
