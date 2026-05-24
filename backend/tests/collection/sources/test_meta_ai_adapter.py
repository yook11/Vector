"""``MetaAISource`` の per-source 単体テスト (P2-D)。

P2-D で identity / 補完方針は ``ClassVar``、取得手順は ``collect(tools)``
classmethod になった。固定する固有不変条件:

- Newsroom feed は全社混在で非 AI category の entry を含む。Source は
  ``is_collectable_meta_ai_entry`` scope predicate を最初に適用し、AI tagged
  entry のみ yield する (business critical 収集スコープの移植証明)
- fixture に AI / 非 AI が両方含まれ、yield 件数 == AI tagged 件数
"""

from __future__ import annotations

from pathlib import Path

import feedparser

from app.collection.article_acquisition.fetched_article import FetchedArticle
from app.collection.article_acquisition.fetcher import fetch_articles
from app.collection.article_acquisition.reader.rss_reader import (
    RssEntry,
    normalize_entry,
)
from app.collection.sources.definitions.meta_ai import (
    MetaAISource,
    is_collectable_meta_ai_entry,
)
from tests.collection.sources._fixture_tools import fixture_tools

_FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures"
_FIXTURE = "meta_ai_rss.xml"


def _raw_entries() -> list[RssEntry]:
    feed = feedparser.parse((_FIXTURES_DIR / _FIXTURE).read_bytes())
    return [normalize_entry(raw) for raw in feed.entries]


async def _collect() -> list[FetchedArticle]:
    tools = fixture_tools(rss_fixture=_FIXTURE)
    return [item async for item in fetch_articles(MetaAISource, tools)]


async def test_only_ai_tagged_entries_are_yielded() -> None:
    raw = _raw_entries()
    ai = [e for e in raw if is_collectable_meta_ai_entry(e)]
    non_ai = [e for e in raw if not is_collectable_meta_ai_entry(e)]
    # fixture が両方含むことを前提に drop を検証する
    assert ai, "fixture must contain at least one AI-tagged entry"
    assert non_ai, "fixture must contain at least one non-AI entry to drop"

    items = await _collect()

    assert len(items) == len(ai)
