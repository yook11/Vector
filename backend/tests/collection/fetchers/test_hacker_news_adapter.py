"""``HackerNewsAdapter`` (Algolia Search API, Pattern H) の不変条件テスト (P2)。

P2 で identity ClassVar を廃し ``source_name`` を ``__init__`` 注入で受ける
(endpoint は API base のため collect() では未使用、identity は
``ArticleSource`` が所有)。検証する不変条件:

- fixture hits から ``ArticleFetcher`` 経由で永続化 passport が yield される
- ``url=None`` の Ask HN 系 hit は yield 自体されない (passport が増えない)
- 空 title の hit は yield されない
- ``HackerNewsApiClient`` の ``ExternalFetchError`` は machinery を素通しする
- ``search_recent_stories`` に renamed kwargs (sliding window / min_points /
  hits_per_page) が必ず渡る (旧仕様: 24h window / points>20 / 100 hits)
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from app.collection.domain.observed_article import ObservedArticle, ObservedOrigin
from app.collection.domain.source_completion_profile import DEFAULT_PROFILE
from app.collection.external_fetch_errors import (
    FetchAccessDeniedError,
    FetchOriginServerError,
)
from app.collection.fetchers.article_fetcher import ArticleFetcher
from app.collection.fetchers.hacker_news import (
    HN_HITS_PER_PAGE,
    HN_MIN_POINTS,
    HN_SLIDING_WINDOW_SECONDS,
    HackerNewsAdapter,
)
from app.collection.fetchers.tools.algolia_hn_client import HackerNewsApiClient
from app.collection.sources.article_source import ArticleSource
from app.shared.value_objects.source_name import SourceName
from tests.collection.fetchers._invariant import (
    Passport,
    assert_at_least_one_passport,
    assert_passports_persistable,
)

_FIXTURE = Path(__file__).parent.parent.parent / "fixtures" / "hacker_news_hits.json"
_HN_NAME = "Hacker News"


def _hits() -> list[dict[str, Any]]:
    raw = json.loads(_FIXTURE.read_text())
    return list(raw["hits"])


class _FakeHNClient(HackerNewsApiClient):
    def __init__(self, hits: list[dict[str, Any]]) -> None:
        self._hits = hits
        self.calls: list[dict[str, Any]] = []

    async def search_recent_stories(
        self,
        *,
        source_name: str,
        min_points: int,
        window_seconds: int,
        hits_per_page: int,
    ) -> list[dict[str, Any]]:
        self.calls.append(
            {
                "source_name": source_name,
                "min_points": min_points,
                "window_seconds": window_seconds,
                "hits_per_page": hits_per_page,
            }
        )
        return self._hits


class _RaisingHNClient(HackerNewsApiClient):
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def search_recent_stories(
        self,
        *,
        source_name: str,  # noqa: ARG002
        min_points: int,  # noqa: ARG002
        window_seconds: int,  # noqa: ARG002
        hits_per_page: int,  # noqa: ARG002
    ) -> list[dict[str, Any]]:
        raise self._exc


async def _collect(it: AsyncIterator[Passport]) -> list[Passport]:
    return [o async for o in it]


def _fetcher(client: HackerNewsApiClient) -> ArticleFetcher:
    """HN machinery を本番同 profile (api+DEFAULT) でラップ。"""
    source = ArticleSource(
        name=SourceName(_HN_NAME),
        endpoint_url="https://hn.algolia.com/api/v1/search_by_date",
        observed_origin=ObservedOrigin.api,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: HackerNewsAdapter(source_name=_HN_NAME, client=client),
    )
    return ArticleFetcher(source)


@pytest.mark.asyncio
async def test_adapter_yields_passports_from_fixture() -> None:
    items = await _collect(_fetcher(_FakeHNClient(_hits())).fetch(source_id=1))
    assert_at_least_one_passport(items)
    # fixture は url 持ち 4 件 / url=None 2 件 → 4 件のみ pass
    pendings = [o for o in items if isinstance(o, ObservedArticle)]
    assert len(pendings) == 4


@pytest.mark.asyncio
async def test_adapter_persistence_invariants() -> None:
    items = await _collect(_fetcher(_FakeHNClient(_hits())).fetch(source_id=1))
    assert_passports_persistable(items)


@pytest.mark.asyncio
async def test_url_none_hits_skipped_in_adapter() -> None:
    """``url=None`` (Ask HN 系) は yield 自体されない (passport にならない)。"""
    only_url_none = [
        {
            "objectID": "x",
            "title": "Ask HN: ...",
            "url": None,
            "created_at": "2026-02-24T17:15:17Z",
        },
        {
            "objectID": "y",
            "title": "Ask HN: empty url",
            "url": "",
            "created_at": "2026-02-24T17:15:17Z",
        },
    ]
    items = await _collect(_fetcher(_FakeHNClient(only_url_none)).fetch(source_id=1))
    assert items == []


@pytest.mark.asyncio
async def test_empty_title_hits_skipped_in_adapter() -> None:
    only_empty_title = [
        {
            "objectID": "x",
            "title": "",
            "url": "https://example.com/foo",
            "created_at": "2026-02-24T17:15:17Z",
        },
    ]
    items = await _collect(_fetcher(_FakeHNClient(only_empty_title)).fetch(source_id=1))
    assert items == []


@pytest.mark.asyncio
async def test_client_kwargs_carry_quality_filters() -> None:
    """Adapter は旧仕様 (24h window / points>20 / 100 hits) を client に渡す。"""
    fake = _FakeHNClient([])
    await _collect(_fetcher(fake).fetch(source_id=1))
    assert fake.calls == [
        {
            "source_name": _HN_NAME,
            "min_points": HN_MIN_POINTS,
            "window_seconds": HN_SLIDING_WINDOW_SECONDS,
            "hits_per_page": HN_HITS_PER_PAGE,
        }
    ]


@pytest.mark.asyncio
async def test_non_recoverable_error_propagates_through_adapter() -> None:
    fetcher = _fetcher(
        _RaisingHNClient(FetchAccessDeniedError(status_code=403, reason="forbidden"))
    )
    with pytest.raises(FetchAccessDeniedError):
        await _collect(fetcher.fetch(source_id=1))


@pytest.mark.asyncio
async def test_recoverable_error_propagates_through_adapter() -> None:
    fetcher = _fetcher(
        _RaisingHNClient(
            FetchOriginServerError(status_code=500, reason="internal_error")
        )
    )
    with pytest.raises(FetchOriginServerError):
        await _collect(fetcher.fetch(source_id=1))
