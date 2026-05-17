"""Frontiers 取得経路 (``FrontiersJournalAdapter`` machinery) の単体テスト。

P2 で Frontiers 4 journal は継承具象を廃し、``FrontiersJournalAdapter``
machinery に per-source config (``source_name`` / ``endpoint_url``) を注入する
形になった (``JOURNAL_NAME`` は取得 logic 非関与の attribution メタだったため
``ArticleSource.name`` に一本化)。

固定する固有不変条件:

- ``description`` が 50 chars 未満の editorial/correction entry は
  ``FrontiersJournalAdapter.collect()`` 内で business critical drop される
  (旧 ``BaseFrontiersFetcher`` の body<50 drop 移植証明)。identity の固定は
  test_source_adapter_profiles に集約。
"""

from __future__ import annotations

from pathlib import Path

import feedparser

from app.collection.fetchers.frontiers._common import FrontiersJournalAdapter
from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.rss_parser import RssEntry, normalize_entry

_FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures"
_FIXTURE = "frontiers_ai_rss.xml"


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


async def test_short_description_entry_is_dropped() -> None:
    """fixture は通常記事 (desc≈613) + Editorial (desc=16) の 2 件。
    body<50 の Editorial は drop され通常記事のみ yield される。"""
    adapter = FrontiersJournalAdapter(
        source_name="Frontiers in Artificial Intelligence",
        endpoint_url=(
            "https://www.frontiersin.org/journals/artificial-intelligence/rss"
        ),
        parser=_FakeRssParser(_FIXTURE),  # type: ignore[arg-type]
    )

    items: list[FetchedArticle] = [item async for item in adapter.collect()]

    assert len(items) == 1
    assert all(item.body is not None and len(item.body) >= 50 for item in items)
    assert not any(item.title.startswith("Editorial:") for item in items)
