"""Cornell Chronicle 用の per-source 取得 config (Pattern H、複数 feed)。

Cornell Chronicle (``https://news.cornell.edu/``) は学部別の
``/taxonomy/term/<id>/feed`` で AI / Computing / Life Sci / Energy / Phys Sci /
Health 等カテゴリ別 RSS を提供する。本体 ``/news/feed`` は site-wide 雑多な
ため採用せず、対象 6 カテゴリのみを巡回する。

P1 までは継承具象 (純 thin subclass) だった。P2 で継承を廃し固有データを
module-level config 化した:

- ``CORNELL_FEEDS``: 6 taxonomy term feed。``ArticleSource.adapter_factory``
  が ``MultiFeedRssAdapter(feeds=CORNELL_FEEDS, parse_mode="bytes", ...)``
  に注入する (feed は Drupal 生成 RSS 2.0 = ``parse_mode="bytes"``)。
- body builder は注入しない (Pattern H 既定 = body なし。description は短い
  概要のみで本文は HTML 取得に委譲)。

identity / 補完方針は ``ArticleSource`` 集約 (``strategy.py``) が所有する。
1 記事が複数 category に tag されるため feed 間 URL 重複が起きるが、
feed 横断 dedup は ``MultiFeedRssAdapter`` machinery が担う。
"""

from __future__ import annotations

from typing import Final

CORNELL_FEEDS: Final[tuple[str, ...]] = (
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
