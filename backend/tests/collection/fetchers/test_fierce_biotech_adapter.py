"""``FierceBiotechAdapter`` machinery の per-source 単体テスト (P2)。

P2 で identity ClassVar を廃し ``endpoint_url`` / ``source_name`` を
``__init__`` 注入で受ける。固定する固有不変条件:

- fixture の ``<pubDate>`` は RFC822 非準拠 ("Apr 30, 2026 6:11pm") で
  feedparser が ``published_parsed=None`` を返すが、Adapter は
  ``_parse_fb_published_at`` strptime fallback (ET→UTC) を適用し具体的な UTC
  値を ``published_at`` に載せる (RFC822 fallback の移植証明)
- Pattern H のため ``body`` は ``None``
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import feedparser

from app.collection.fetchers.fierce_biotech import FierceBiotechAdapter
from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.rss_parser import RssEntry, normalize_entry

_FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures"
_FIXTURE = "fierce_biotech_rss.xml"


class _FakeRssParser:
    def __init__(self, fixture_filename: str) -> None:
        self._fixture_filename = fixture_filename

    async def fetch(
        self,
        *,
        endpoint_url: str,
        source_name: str,
        parse_mode: str = "text",
        **_: object,
    ) -> list[RssEntry]:
        path = _FIXTURES_DIR / self._fixture_filename
        feed = feedparser.parse(path.read_bytes())
        return [normalize_entry(raw) for raw in feed.entries]


def _adapter() -> FierceBiotechAdapter:
    return FierceBiotechAdapter(
        endpoint_url="https://www.fiercebiotech.com/rss/xml",
        source_name="FierceBiotech",
        parser=_FakeRssParser(_FIXTURE),  # type: ignore[arg-type]
    )


async def _collect(adapter: FierceBiotechAdapter) -> list[FetchedArticle]:
    return [item async for item in adapter.collect()]


async def test_non_rfc822_pubdate_recovered_via_strptime_fallback() -> None:
    """ "Apr 30, 2026 6:11pm" (ET, EDT=UTC-4) → 2026-04-30 22:11 UTC。"""
    items = await _collect(_adapter())

    assert items
    assert items[0].published_at == datetime(2026, 4, 30, 22, 11, tzinfo=UTC)


async def test_body_is_none_pattern_h() -> None:
    items = await _collect(_adapter())

    assert items
    assert all(item.body is None for item in items)
