"""``TheRegisterAdapter`` の per-source 単体テスト (HTTP 非依存)。

固定する固有不変条件:

- ``<link href>`` が redirector (``go.theregister.com/feed/...``) の entry が
  fixture に含まれるが、yield される ``FetchedArticle.url`` は実 host へ展開され
  ``go.theregister.com/feed/`` を一切含まない (redirector 正規化の移植証明)
- Pattern H のため ``body`` は ``None``
- ``NAME`` / ``ENDPOINT_URL`` が class attr として読める
"""

from __future__ import annotations

from pathlib import Path

import feedparser

from app.collection.fetchers.the_register import TheRegisterAdapter
from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.rss_parser import RssEntry, normalize_entry

_FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures"
_FIXTURE = "the_register_atom.xml"


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


async def _collect(adapter: TheRegisterAdapter) -> list[FetchedArticle]:
    return [item async for item in adapter.collect()]


async def test_redirector_links_are_expanded_to_real_host() -> None:
    adapter = TheRegisterAdapter(parser=_FakeRssParser(_FIXTURE))  # type: ignore[arg-type]

    items = await _collect(adapter)

    assert items
    for item in items:
        assert "go.theregister.com/feed/" not in item.url, item.url
        assert item.url.startswith("https://"), item.url


async def test_body_is_none_pattern_h() -> None:
    adapter = TheRegisterAdapter(parser=_FakeRssParser(_FIXTURE))  # type: ignore[arg-type]

    items = await _collect(adapter)

    assert items
    assert all(item.body is None for item in items)


def test_exposes_name_and_endpoint_url() -> None:
    assert TheRegisterAdapter.NAME == "The Register"
    assert (
        TheRegisterAdapter.ENDPOINT_URL == "https://www.theregister.com/headlines.atom"
    )
