"""``TheRegisterSource`` の per-source 単体テスト (P2-D)。

P2-D で identity / 補完方針は ``ClassVar``、取得手順は ``collect(tools)``
classmethod になった。固定する固有不変条件:

- ``<link href>`` が redirector (``go.theregister.com/feed/...``) の entry が
  fixture に含まれるが、yield される ``FetchedArticle.url`` は実 host へ展開され
  ``go.theregister.com/feed/`` を一切含まない (redirector 正規化の移植証明)
- Pattern H のため ``body`` は ``None``
"""

from __future__ import annotations

from app.collection.source_fetch.fetched_article import FetchedArticle
from app.collection.sources.definitions.the_register import TheRegisterSource
from tests.collection.fetchers._fixture_tools import fixture_tools

_FIXTURE = "the_register_atom.xml"


async def _collect() -> list[FetchedArticle]:
    tools = fixture_tools(rss_fixture=_FIXTURE)
    return [item async for item in TheRegisterSource.collect(tools)]


async def test_redirector_links_are_expanded_to_real_host() -> None:
    items = await _collect()

    assert items
    for item in items:
        assert "go.theregister.com/feed/" not in item.url, item.url
        assert item.url.startswith("https://"), item.url


async def test_body_is_none_pattern_h() -> None:
    items = await _collect()

    assert items
    assert all(item.body is None for item in items)
