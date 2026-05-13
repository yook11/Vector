"""Anthropic 用 Fetcher — Phase 3 PR 3-d-4 (sitemap-only Pattern H)。

Anthropic は ``/rss.xml`` ``/feed`` ``/news/rss.xml`` 全て 404 で RSS を一切
提供せず、唯一 ``/sitemap.xml`` のみが利用可能。``BaseSitemapFetcher`` を
そのまま採用し、``URL_PATH_PREFIX = "/news/"`` で news セクションのみに
絞り込む (about / pricing 等は除外)。

attribution は Anthropic 公式の標準利用規約相当文言が無いため、source name
``"Anthropic"`` を ``news_sources.attribution_label`` に詰める (DB 行は
alembic ``o3_add_anthropic`` で seed)。

robots.txt: ``User-agent: *`` で ``Allow: /`` blanket + ``Sitemap:`` 明示。
"""

from __future__ import annotations

from typing import ClassVar

from app.collection.fetchers._base.sitemap import BaseSitemapFetcher


class AnthropicFetcher(BaseSitemapFetcher):
    """Anthropic news の sitemap-only Fetcher。

    PROVIDES は基底の ``{"site_name", "language"}`` をそのまま継承。``guid``
    は loc URL でほぼ確定だが、契約として宣言するほど安定的でないため
    PROVIDES には含めない (``metadata["guid"]`` には毎回詰める)。
    """

    NAME: ClassVar[str] = "Anthropic"
    ENDPOINT_URL: ClassVar[str] = "https://www.anthropic.com/sitemap.xml"
    URL_PATH_PREFIX: ClassVar[str] = "/news/"
    SITE_NAME: ClassVar[str] = "Anthropic"
    LANGUAGE: ClassVar[str] = "en"
