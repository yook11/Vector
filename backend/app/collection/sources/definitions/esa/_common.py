"""ESA Djangoplicity 規格 RSS の取得共通処理 (P2-D)。

Djangoplicity の News module は ESA/Hubble / ESA/Webb / ESO / ALMA で広く
使われる科学広報 CMS。RSS 出力は構造的に同型:

- RSS 2.0 (UTF-8、CET/CEST timezone の pubDate)
- ``<item>`` は ``<title>`` (CDATA, "Photo Release:" / "Science Release:" 等
  の prefix を含む) / ``<link>`` (絶対 URL) / ``<guid>`` (link と同値) /
  ``<pubDate>`` (RFC 822 +0100/+0200) / ``<description>`` (HTML の lead
  paragraph、~500-900 chars)
- ``<author>`` / ``<dc:creator>`` / ``<media:*>`` は **未提供**
- 本文は HTML 詳細ページに委譲 (Pattern H)

P1 まで: 継承基底で subclass が ``NAME`` / ``ENDPOINT_URL`` ClassVar を差替。
P2(B+C): ``DjangoplicityAdapter`` 汎用 machinery クラス。
P2-D (本実装): Adapter 概念除去。本モジュールは **free function**
``djangoplicity_entries(tools, *, source_name, endpoint_url)`` として共通処理
だけを持つ。具体 Source (``ESAHubbleSource`` / ``ESAWebbSource``) は
``esa/sources.py`` が宣言し、その ``collect`` が本関数へ委譲する
(``_common.py`` に source-specific な事実を残さない)。

判定順は旧 ``BaseDjangoplicityFetcher._convert_entry`` を踏襲: title 空のみ
structural gate (URL canonical は ``passport_builder`` に委譲)。本文は HTML
詳細ページに委譲する Pattern H のため ``body=None`` で渡す。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.collection.source_fetch.fetched_article import FetchedArticle
from app.collection.source_fetch.tools.fetch_tools import FetchTools


async def djangoplicity_entries(
    tools: FetchTools,
    *,
    source_name: str,
    endpoint_url: str,
) -> AsyncIterator[FetchedArticle]:
    """ESA Djangoplicity News module RSS の Pattern H 取得共通処理。"""
    entries = await tools.rss.fetch(
        endpoint_url=endpoint_url,
        source_name=source_name,
        parse_mode="bytes",
    )
    for entry in entries:
        title = entry.title[:500]
        if not title:
            continue
        yield FetchedArticle(
            title=title,
            url=entry.link,
            body=None,
            published_at=entry.published,
        )
