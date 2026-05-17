"""``MicrosoftResearchSource`` の per-source 単体テスト (P2-D)。

P2-D で identity / 補完方針は ``ClassVar``、取得手順は ``collect(tools)``
classmethod になった。固定する固有不変条件:

- fixture の ``<content:encoded>`` に WordPress 固定 footer
  ("... appeared first on Microsoft Research.") が付くが、yield される
  ``FetchedArticle.body`` には footer が残らない (footer regex strip の移植証明)
"""

from __future__ import annotations

from pathlib import Path

from app.collection.fetchers.microsoft_research import MicrosoftResearchSource
from app.collection.fetchers.tools.fetched_article import FetchedArticle
from tests.collection.fetchers._fixture_tools import fixture_tools

_FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures"
_FIXTURE = "microsoft_research_rss.xml"
_FOOTER_MARKER = "appeared first on Microsoft Research"


async def _collect() -> list[FetchedArticle]:
    tools = fixture_tools(rss_fixture=_FIXTURE)
    return [item async for item in MicrosoftResearchSource.collect(tools)]


async def test_fixture_actually_contains_footer() -> None:
    """fixture が footer を含むことを前提条件として固定する。"""
    raw = (_FIXTURES_DIR / _FIXTURE).read_bytes().decode("utf-8", "ignore")
    assert _FOOTER_MARKER in raw


async def test_footer_is_stripped_from_body() -> None:
    items = await _collect()

    assert items
    for item in items:
        assert item.body is not None
        assert _FOOTER_MARKER not in item.body
