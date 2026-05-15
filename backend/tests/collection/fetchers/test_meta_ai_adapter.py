"""``MetaAIAdapter`` の per-source 単体テスト (HTTP 非依存)。

固定する固有不変条件:

- Newsroom feed は全社混在で非 AI category の entry を含む。Adapter は
  ``_is_ai_tagged`` フィルタを最初に適用し、AI tagged entry のみ yield する
  (business critical drop の移植証明)
- fixture に AI / 非 AI が両方含まれ、yield 件数 == AI tagged 件数
- ``NAME`` / ``ENDPOINT_URL`` が class attr として読める
"""

from __future__ import annotations

from pathlib import Path

import feedparser

from app.collection.fetchers.meta_ai import MetaAIAdapter, _is_ai_tagged
from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.rss_parser import RssEntry, normalize_entry

_FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures"
_FIXTURE = "meta_ai_rss.xml"


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


def _raw_entries() -> list[RssEntry]:
    feed = feedparser.parse((_FIXTURES_DIR / _FIXTURE).read_bytes())
    return [normalize_entry(raw) for raw in feed.entries]


async def _collect(adapter: MetaAIAdapter) -> list[FetchedArticle]:
    return [item async for item in adapter.collect()]


async def test_only_ai_tagged_entries_are_yielded() -> None:
    raw = _raw_entries()
    ai = [e for e in raw if _is_ai_tagged(e.tags)]
    non_ai = [e for e in raw if not _is_ai_tagged(e.tags)]
    # fixture が両方含むことを前提に drop を検証する
    assert ai, "fixture must contain at least one AI-tagged entry"
    assert non_ai, "fixture must contain at least one non-AI entry to drop"

    adapter = MetaAIAdapter(parser=_FakeRssParser(_FIXTURE))  # type: ignore[arg-type]
    items = await _collect(adapter)

    assert len(items) == len(ai)


def test_exposes_name_and_endpoint_url() -> None:
    assert MetaAIAdapter.NAME == "Meta AI"
    assert MetaAIAdapter.ENDPOINT_URL == "https://about.fb.com/news/feed/"
