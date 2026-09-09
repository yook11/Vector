"""TechCrunchのRSS取得宣言。"""

from typing import ClassVar

from app.collection.domain.observed_article import ObservedOrigin
from app.collection.sources.article_completion_policy import (
    DEFAULT_POLICY,
    ArticleCompletionPolicy,
)
from app.collection.sources.fetch_cadence import FetchCadence
from app.collection.sources.rss_acquisition import RssAcquisition, RssBodyPolicy
from app.collection.sources.source_name import SourceName


class TechCrunchSource:
    name: ClassVar[SourceName] = SourceName("TechCrunch")
    acquisition: ClassVar[RssAcquisition] = RssAcquisition(
        feeds=("https://techcrunch.com/feed/",),
        parse_mode="text",
        body_policy=RssBodyPolicy.DISCARD,
    )
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_policy: ClassVar[ArticleCompletionPolicy] = DEFAULT_POLICY
    fetch_cadence: ClassVar[FetchCadence] = FetchCadence.HIGH
