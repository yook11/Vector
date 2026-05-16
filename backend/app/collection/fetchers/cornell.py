"""Cornell Chronicle 用 Fetcher — Pattern H、6 taxonomy term feed 巡回。

Cornell Chronicle (``https://news.cornell.edu/``) は学部別の
``/taxonomy/term/<id>/feed`` で AI / Computing / Life Sci / Energy / Phys Sci /
Health 等カテゴリ別 RSS を提供する。本体 ``/news/feed`` は site-wide 雑多な
ため採用せず、対象 6 カテゴリのみを ``FEEDS`` ClassVar で巡回する。

per-source 設計:

- feed が **RSS 2.0** (UTF-8、Drupal 生成器) → ``PARSE_MODE = "bytes"``
- description は短い概要のみ → Pattern H (本文は HTML 取得に委譲)。
  ``_build_body`` を override しない (基底既定 ``None``)

複数 feed 巡回 (``BaseMultiFeedRssAdapter`` に集約):

- 6 taxonomy term feed を ``FEEDS`` ClassVar で保持 (NASA fetcher と同設計)
- 1 feed の ``ExternalFetchError`` は **種類問わず** ``source_feed_fetch_failed``
  warning に記録して次 feed へ進む。全 feed 失敗時のみ最初の error を
  source 全体失敗として伝播する (詳細は ``BaseMultiFeedRssAdapter``)
- 1 記事が複数 category に tag されるため feed 間で URL 重複が発生する。
  feed 横断 ``seen_urls`` dedup も基底が担う
"""

from __future__ import annotations

from typing import ClassVar

from app.collection.fetchers.tools.multi_feed_rss import BaseMultiFeedRssAdapter
from app.collection.fetchers.tools.rss_parser import ParseMode


class CornellChronicleAdapter(BaseMultiFeedRssAdapter):
    """Cornell Chronicle 用 SourceAdapter (Pattern H、6 feed 巡回 + URL dedup)。

    純 thin subclass (``BaseDjangoplicityAdapter`` / ``MDPIEnergiesAdapter``
    と同形)。per-feed 失敗隔離・feed 横断 dedup・全 feed 失敗時 surface は
    ``BaseMultiFeedRssAdapter`` が一括で担う。
    """

    NAME: ClassVar[str] = "Cornell Chronicle"
    ENDPOINT_URL: ClassVar[str] = "https://news.cornell.edu/taxonomy/term/24043/feed"
    FEEDS: ClassVar[tuple[str, ...]] = (
        # Artificial Intelligence
        "https://news.cornell.edu/taxonomy/term/24043/feed",
        # Computing & Information Sciences
        "https://news.cornell.edu/taxonomy/term/14256/feed",
        # Life Sciences & Veterinary Medicine
        "https://news.cornell.edu/taxonomy/term/15056/feed",
        # Energy, Environment & Sustainability
        "https://news.cornell.edu/taxonomy/term/15621/feed",
        # Physical Sciences & Engineering
        "https://news.cornell.edu/taxonomy/term/14252/feed",
        # Health, Nutrition & Medicine
        "https://news.cornell.edu/taxonomy/term/14248/feed",
    )
    PARSE_MODE: ClassVar[ParseMode] = "bytes"
