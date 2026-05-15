"""``MicrosoftResearchAdapter`` の per-source 単体テスト (HTTP 非依存)。

固定する固有不変条件:

- fixture の ``<content:encoded>`` に WordPress 固定 footer
  ("... appeared first on Microsoft Research.") が付くが、yield される
  ``FetchedArticle.body`` には footer が残らない (footer regex strip の移植証明)
- ``NAME`` / ``ENDPOINT_URL`` が class attr として読める
"""

from __future__ import annotations

from pathlib import Path

import feedparser

from app.collection.fetchers.microsoft_research import MicrosoftResearchAdapter
from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.rss_parser import RssEntry, normalize_entry

_FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures"
_FIXTURE = "microsoft_research_rss.xml"
_FOOTER_MARKER = "appeared first on Microsoft Research"


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


async def _collect(adapter: MicrosoftResearchAdapter) -> list[FetchedArticle]:
    return [item async for item in adapter.collect()]


async def test_fixture_actually_contains_footer() -> None:
    """fixture が footer を含むことを前提条件として固定する。"""
    raw = (_FIXTURES_DIR / _FIXTURE).read_bytes().decode("utf-8", "ignore")
    assert _FOOTER_MARKER in raw


async def test_footer_is_stripped_from_body() -> None:
    adapter = MicrosoftResearchAdapter(parser=_FakeRssParser(_FIXTURE))  # type: ignore[arg-type]

    items = await _collect(adapter)

    assert items
    for item in items:
        assert item.body is not None
        assert _FOOTER_MARKER not in item.body


def test_exposes_name_and_endpoint_url() -> None:
    assert MicrosoftResearchAdapter.NAME == "Microsoft Research"
    assert (
        MicrosoftResearchAdapter.ENDPOINT_URL
        == "https://www.microsoft.com/en-us/research/feed/"
    )
