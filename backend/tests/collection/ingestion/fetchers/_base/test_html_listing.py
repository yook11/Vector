"""``BaseHtmlListingFetcher`` の振る舞い不変条件テスト (Phase 3 PR 3-i-1)。

検証する不変条件:

- listing HTML から XPath で /news/ 配下の link だけが抽出される
- ``EXCLUDED_PATHS`` の URL は yield されず下流に流れない
- 同 URL の重複は ``fetch()`` で dedup されて 1 件に集約
- ``MAX_ENTRIES`` で yield 件数が cap される (大量バックフィル防止)
- 不正 URL は ``Failed`` で個別 drop され全体停止しない
- ``_convert_entry`` は ``prefer_html_title=True`` の passport を返す
  (title 確定は HTML 抽出 task の責務)
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from app.collection.ingestion.domain.fetched_article import (
    Failed,
    FetchedEntry,
    PendingHtmlFetch,
)
from app.collection.ingestion.fetchers._base.html_listing import BaseHtmlListingFetcher
from tests.collection.ingestion.fetchers._invariant import (
    assert_at_least_one_passport,
    assert_metadata_audit_safe,
    assert_passports_persistable,
    assert_provides_contract,
)


class _SampleFetcher(BaseHtmlListingFetcher):
    NAME: ClassVar[str] = "Sample"
    ENDPOINT_URL: ClassVar[str] = "https://example.com/news"
    LISTING_URL: ClassVar[str] = "https://example.com/news"
    DETAIL_LINK_XPATH: ClassVar[str] = '//a[starts-with(@href, "/news/")]'
    DETAIL_URL_PREFIX: ClassVar[str] = "https://example.com"
    SITE_NAME: ClassVar[str] = "Sample"
    LANGUAGE: ClassVar[str] = "en"
    EXCLUDED_PATHS: ClassVar[frozenset[str]] = frozenset({"/news/categories"})
    MAX_ENTRIES: ClassVar[int] = 10


_FIXTURE_HTML = b"""<!DOCTYPE html>
<html><body>
  <a href="/news/categories">Categories landing (excluded)</a>
  <a href="/news/article-one">Article One</a>
  <a href="/news/article-two">Article Two</a>
  <a href="/news/article-one">Duplicate of Article One</a>
  <a href="https://example.com/external">External link (XPath excludes)</a>
</body></html>
"""


def _outcomes() -> list:
    fetcher = _SampleFetcher()
    urls = _SampleFetcher._parse_listing(_FIXTURE_HTML)
    seen: set[str] = set()
    out: list = []
    for url in urls:
        if url in seen or not fetcher._url_matches(url):
            continue
        seen.add(url)
        out.append(fetcher._convert_entry(url, 1))
    return out


def test_passports_satisfy_persistence_invariants() -> None:
    assert_at_least_one_passport(_outcomes())
    assert_passports_persistable(_outcomes())


def test_provides_contract_holds() -> None:
    assert_provides_contract(_outcomes(), _SampleFetcher.PROVIDES)


def test_metadata_audit_safe() -> None:
    assert_metadata_audit_safe(_outcomes())


def test_invalid_url_isolated_to_failed() -> None:
    """1 entry の invalid URL は他 entry を巻き込まない (部分回復契約)。"""
    outcome = _SampleFetcher()._convert_entry("not-a-url", 1)
    assert isinstance(outcome, Failed)
    assert outcome.reason.code == "extraction_empty"


@pytest.mark.asyncio
async def test_fetch_dedups_and_excludes(monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch() ループは EXCLUDED_PATHS / 重複を取り除いた passport だけ yield する。"""
    fetcher = _SampleFetcher()

    async def _fake_fetch_listing(self: _SampleFetcher) -> bytes:
        return _FIXTURE_HTML

    monkeypatch.setattr(
        _SampleFetcher, "_fetch_listing", _fake_fetch_listing, raising=True
    )

    outcomes = [o async for o in fetcher.fetch(1)]
    assert len(outcomes) == 2
    urls = {
        str(o.item.source_url)
        for o in outcomes
        if isinstance(o, FetchedEntry) and isinstance(o.item, PendingHtmlFetch)
    }
    assert urls == {
        "https://example.com/news/article-one",
        "https://example.com/news/article-two",
    }


@pytest.mark.asyncio
async def test_fetch_respects_max_entries_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """MAX_ENTRIES = 5 のとき 50 link → 5 件で打ち切り。"""
    many_links = b"".join(
        f'<a href="/news/article-{i}">A{i}</a>'.encode() for i in range(50)
    )
    many_html = b"<!DOCTYPE html><html><body>" + many_links + b"</body></html>"

    class _Capped(_SampleFetcher):
        MAX_ENTRIES: ClassVar[int] = 5

    async def _fake_fetch_listing(self: _Capped) -> bytes:
        return many_html

    monkeypatch.setattr(_Capped, "_fetch_listing", _fake_fetch_listing, raising=True)
    outcomes = [o async for o in _Capped().fetch(1)]
    assert len(outcomes) == 5
