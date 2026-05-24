"""``FierceBiotechSource`` の per-source 単体テスト (P2-D)。

P2-D で identity / 補完方針は ``ClassVar``、取得手順は ``collect(tools)``
classmethod になった。固定する固有不変条件:

- fixture の ``<pubDate>`` は RFC822 非準拠 ("Apr 30, 2026 6:11pm") で
  feedparser が ``published_parsed=None`` を返すが、Source は
  ``_parse_fb_published_at`` strptime fallback (ET→UTC) を適用し具体的な UTC
  値を ``published_at`` に載せる (RFC822 fallback の移植証明)
- Pattern H のため ``body`` は ``None``
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.collection.article_acquisition.fetched_article import FetchedArticle
from app.collection.article_acquisition.fetcher import fetch_articles
from app.collection.sources.definitions.fierce_biotech import FierceBiotechSource
from tests.collection.sources._fixture_tools import fixture_tools

_FIXTURE = "fierce_biotech_rss.xml"


async def _collect() -> list[FetchedArticle]:
    tools = fixture_tools(rss_fixture=_FIXTURE)
    return [item async for item in fetch_articles(FierceBiotechSource, tools)]


async def test_non_rfc822_pubdate_recovered_via_strptime_fallback() -> None:
    """ "Apr 30, 2026 6:11pm" (ET, EDT=UTC-4) → 2026-04-30 22:11 UTC。"""
    items = await _collect()

    assert items
    assert items[0].published_at == datetime(2026, 4, 30, 22, 11, tzinfo=UTC)


async def test_body_is_none_pattern_h() -> None:
    items = await _collect()

    assert items
    assert all(item.body is None for item in items)
