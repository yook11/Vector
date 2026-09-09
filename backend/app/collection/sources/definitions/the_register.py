"""The RegisterのRSS取得宣言。"""

from typing import ClassVar

from app.collection.domain.observed_article import ObservedOrigin
from app.collection.sources.article_completion_policy import (
    DEFAULT_POLICY,
    ArticleCompletionPolicy,
)
from app.collection.sources.fetch_cadence import FetchCadence
from app.collection.sources.rss_acquisition import RssAcquisition, RssBodyPolicy
from app.collection.sources.source_name import SourceName

_REDIRECTOR_PREFIX = "https://go.theregister.com/feed/"


class TheRegisterSource:
    name: ClassVar[SourceName] = SourceName("The Register")
    acquisition: ClassVar[RssAcquisition] = RssAcquisition(
        feeds=("https://www.theregister.com/headlines.atom",),
        parse_mode="text",
        body_policy=RssBodyPolicy.DISCARD,
    )
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_policy: ClassVar[ArticleCompletionPolicy] = DEFAULT_POLICY
    fetch_cadence: ClassVar[FetchCadence] = FetchCadence.HIGH

    @staticmethod
    def transform_url(url: str) -> str:
        """配信用redirectorのURLを記事URLへ展開する。"""
        if url.startswith(_REDIRECTOR_PREFIX):
            return "https://" + url[len(_REDIRECTOR_PREFIX) :]
        return url
