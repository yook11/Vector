"""NASA 用の per-source 取得 config (Pattern R、複数 feed)。

P1 までは継承具象で per-source 定数 (``FEEDS``) と Pattern R 拡張点
(本文 override) を保持していた。P2 で継承を廃し、固有データを module-level
config 化した:

- ``NASA_FEEDS``: 6 feed (本体 + news-release / technology / aeronautics /
  station / artemis)。``ArticleSource.adapter_factory`` が
  ``MultiFeedRssAdapter(feeds=NASA_FEEDS, ...)`` に注入する。
- ``nasa_build_body``: body は ``entry.content_encoded``
  (``<content:encoded>``) を ``_strip_html`` で plain text 化して直取り
  (nav noise 含むまま、Stage 2 LLM 側で吸収する設計 = Pattern R)。
  ``MultiFeedRssAdapter(body_builder=nasa_build_body, ...)`` に注入する。

identity / 補完方針 (``name`` / ``endpoint_url`` / ``observed_origin`` /
``completion_profile``) は ``ArticleSource`` 集約 (``strategy.py``) が所有する。
per-feed 失敗隔離・feed 横断 dedup・全 feed 失敗時 surface は
``MultiFeedRssAdapter`` machinery が一括で担う。
"""

from __future__ import annotations

import html
import re
from typing import Final

from app.collection.fetchers.tools.rss_parser import RssEntry

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")

NASA_FEEDS: Final[tuple[str, ...]] = (
    "https://www.nasa.gov/feed/",
    "https://www.nasa.gov/news-release/feed/",
    "https://www.nasa.gov/technology/feed/",
    "https://www.nasa.gov/aeronautics/feed/",
    "https://www.nasa.gov/missions/station/feed/",
    "https://www.nasa.gov/missions/artemis/feed/",
)


def _strip_html(s: str) -> str:
    """HTML タグを剥がして plain text に正規化する (body 用)。"""
    if not s:
        return ""
    return _WHITESPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", s))).strip()


def nasa_build_body(entry: RssEntry) -> str | None:
    """Pattern R: ``content_encoded`` を plain text 化して本文に採用する。"""
    return _strip_html(entry.content_encoded or "") or None
