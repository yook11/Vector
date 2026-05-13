"""ORNL (Oak Ridge National Laboratory) 用 Fetcher — Phase 3 PR 3-i-1。

RSS / Atom / sitemap.xml を提供しないため、``/news`` listing ページから記事
URL を列挙する Pattern H 経路。``BaseHtmlListingFetcher`` の初導入 subclass。

per-source 設計 (実 listing 観察ベース、SSoT: tier1-fetcher-research.md §PR 3-i-1):

- listing URL: ``https://www.ornl.gov/news`` (200 OK、UTF-8、~64KB)
- detail link 抽出: ``//a[starts-with(@href, "/news/")]`` で 17 件取得
  (うち 6 件は category landing、11 件が記事)
- category landing 除外: ``EXCLUDED_PATHS`` で path 単位の denylist。
  XPath 内の ``not()`` 多用は読みづらく将来の category 追加時に脆い。
- robots.txt: /news/ 配下を許可、Crawl-delay 10s
  (本 PR では ``extract_html_body`` task 側の host-level rate limiter で
  対応、本基底では in-process sleep しない。TODO は base docstring 参照)
- License: U.S. Government work、attribution_label = "ORNL · DOE"
  (UI 側で full credit "Courtesy of Oak Ridge National Laboratory,
  U.S. Department of Energy" に template 展開する)
"""

from __future__ import annotations

from typing import ClassVar

from app.collection.fetchers._base.html_listing import BaseHtmlListingFetcher


class ORNLNewsFetcher(BaseHtmlListingFetcher):
    """ORNL (Oak Ridge National Laboratory) news listing fetcher。"""

    NAME: ClassVar[str] = "ORNL"
    ENDPOINT_URL: ClassVar[str] = "https://www.ornl.gov/news"
    LISTING_URL: ClassVar[str] = "https://www.ornl.gov/news"
    DETAIL_LINK_XPATH: ClassVar[str] = '//a[starts-with(@href, "/news/")]'
    DETAIL_URL_PREFIX: ClassVar[str] = "https://www.ornl.gov"
    SITE_NAME: ClassVar[str] = "ORNL"
    LANGUAGE: ClassVar[str] = "en"
    # ``/news`` 配下の category landing ページ 6 件を除外。
    # 2026-05-04 時点の実 listing で確認 (releases / features /
    # researcher-profiles / story-tips / audio-spots / honors-and-awards)。
    EXCLUDED_PATHS: ClassVar[frozenset[str]] = frozenset(
        {
            "/news/releases",
            "/news/features",
            "/news/researcher-profiles",
            "/news/story-tips",
            "/news/audio-spots",
            "/news/honors-and-awards",
        }
    )
