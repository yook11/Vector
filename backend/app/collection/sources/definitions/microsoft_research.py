"""Microsoft ResearchのRSS取得宣言。"""

import re
from typing import ClassVar

from app.collection.domain.observed_article import ObservedOrigin
from app.collection.sources.article_completion_policy import (
    DEFAULT_POLICY,
    ArticleCompletionPolicy,
)
from app.collection.sources.fetch_cadence import FetchCadence
from app.collection.sources.rss_acquisition import RssAcquisition, RssBodyPolicy
from app.collection.sources.source_name import SourceName

_FOOTER_RE = re.compile(
    r"\s*Opens in a new tab\s*The post .* appeared first on Microsoft Research\.\s*$",
    re.DOTALL,
)


class MicrosoftResearchSource:
    name: ClassVar[SourceName] = SourceName("Microsoft Research")
    acquisition: ClassVar[RssAcquisition] = RssAcquisition(
        feeds=("https://www.microsoft.com/en-us/research/feed/",),
        parse_mode="text",
        body_policy=RssBodyPolicy.CONTENT_ENCODED,
    )
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_policy: ClassVar[ArticleCompletionPolicy] = DEFAULT_POLICY
    fetch_cadence: ClassVar[FetchCadence] = FetchCadence.MEDIUM

    @staticmethod
    def transform_body(body: str) -> str:
        """本文末尾の配信元フッターを除去する。"""
        return _FOOTER_RE.sub("", body)
