"""NASA 用 Fetcher — Pattern R (RSS-only)、複数 feed 巡回 + URL dedup。

per-source 設計:

- body は ``entry.content_encoded`` (``<content:encoded>``) を**直取り**
  (nav noise 含むまま、Stage 2 LLM 側で吸収する設計)。``_build_body`` を
  override して ``_strip_html`` で plain text 化する

複数 feed 巡回 (``BaseMultiFeedRssAdapter`` に集約):

- 6 feed (本体 + news-release / technology / aeronautics / station / artemis)
  を ``FEEDS`` ClassVar で保持
- 1 feed の ``ExternalFetchError`` は **種類問わず** ``source_feed_fetch_failed``
  warning に記録して次 feed へ進む。全 feed 失敗時のみ最初の error を
  source 全体失敗として伝播する (詳細は ``BaseMultiFeedRssAdapter``)
- feed 横断 ``seen_urls`` dedup も基底が担う
"""

from __future__ import annotations

import html
import re
from typing import ClassVar

from app.collection.fetchers.tools.multi_feed_rss import BaseMultiFeedRssAdapter
from app.collection.fetchers.tools.rss_parser import RssEntry

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _strip_html(s: str) -> str:
    """HTML タグを剥がして plain text に正規化する (body 用)。"""
    if not s:
        return ""
    return _WHITESPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", s))).strip()


class NASAAdapter(BaseMultiFeedRssAdapter):
    """NASA 用 SourceAdapter (Pattern R、6 feed 巡回 + URL dedup)。

    Pattern R のため ``_build_body`` を override し ``content_encoded`` を
    plain text 化して渡す。per-feed 失敗隔離・feed 横断 dedup・全 feed
    失敗時 surface は ``BaseMultiFeedRssAdapter`` が一括で担う
    (``PARSE_MODE`` は基底既定 ``"text"`` を継承)。
    """

    NAME: ClassVar[str] = "NASA"
    ENDPOINT_URL: ClassVar[str] = "https://www.nasa.gov/feed/"
    FEEDS: ClassVar[tuple[str, ...]] = (
        "https://www.nasa.gov/feed/",
        "https://www.nasa.gov/news-release/feed/",
        "https://www.nasa.gov/technology/feed/",
        "https://www.nasa.gov/aeronautics/feed/",
        "https://www.nasa.gov/missions/station/feed/",
        "https://www.nasa.gov/missions/artemis/feed/",
    )

    def _build_body(self, entry: RssEntry) -> str | None:
        return _strip_html(entry.content_encoded or "") or None
