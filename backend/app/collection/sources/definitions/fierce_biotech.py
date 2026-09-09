"""FierceBiotechのRSS取得宣言。"""

from datetime import UTC, datetime
from typing import ClassVar
from zoneinfo import ZoneInfo

from app.collection.article_acquisition.reader.rss_reader import RssEntry
from app.collection.domain.observed_article import ObservedOrigin
from app.collection.sources.article_completion_policy import (
    DEFAULT_POLICY,
    ArticleCompletionPolicy,
)
from app.collection.sources.fetch_cadence import FetchCadence
from app.collection.sources.rss_acquisition import RssAcquisition, RssBodyPolicy
from app.collection.sources.source_name import SourceName

_FB_PUBDATE_FORMAT = "%b %d, %Y %I:%M%p"
# 配信元の日時にはtimezoneが無いため既存の米国東部時間の解釈を維持する。
_FB_TZ = ZoneInfo("America/New_York")


class FierceBiotechSource:
    name: ClassVar[SourceName] = SourceName("FierceBiotech")
    acquisition: ClassVar[RssAcquisition] = RssAcquisition(
        feeds=("https://www.fiercebiotech.com/rss/xml",),
        parse_mode="text",
        body_policy=RssBodyPolicy.DISCARD,
    )
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_policy: ClassVar[ArticleCompletionPolicy] = DEFAULT_POLICY
    fetch_cadence: ClassVar[FetchCadence] = FetchCadence.MEDIUM

    @staticmethod
    def resolve_published_at(entry: RssEntry) -> datetime | None:
        """標準解析で日時が得られない場合だけ配信元固有の形式を解釈する。"""
        if entry.published is not None:
            return entry.published
        raw = entry.raw_published or entry.raw_updated
        if not raw:
            return None
        try:
            dt = datetime.strptime(raw.strip(), _FB_PUBDATE_FORMAT)  # noqa: DTZ007
        except ValueError:
            return None
        return dt.replace(tzinfo=_FB_TZ).astimezone(UTC)
