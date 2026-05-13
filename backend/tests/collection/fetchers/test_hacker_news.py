"""``HackerNewsFetcher`` (API Pattern H) の不変条件テスト。

検証する不変条件:

- API hit を ``_convert_hit`` した結果が永続化 passport の不変条件を満たす
- ``url`` 欠落 (Ask HN 系) は yield せず None で skip (永続化に流さない)
- HTTP error は分類済みの ``PermanentFetchError`` / ``TemporaryFetchError`` に
  変換される (上流の retry 判断を狂わせない)
- API request の必須パラメタ (sliding window / 最低 points / story tag) を必ず付ける
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.collection.errors import PermanentFetchError, TemporaryFetchError
from app.collection.fetchers.hacker_news import (
    HN_HITS_PER_PAGE,
    HN_MIN_POINTS,
    HN_SLIDING_WINDOW_SECONDS,
    HackerNewsFetcher,
)
from app.collection.fetchers.outcome import (
    FetchedEntry,
    FetchOutcome,
    SourceFetchFailed,
)
from app.collection.incomplete_article.domain.incomplete_article import (
    IncompleteArticle,
)
from tests.collection.fetchers._invariant import (
    assert_at_least_one_passport,
    assert_metadata_audit_safe,
    assert_passports_persistable,
    assert_provides_contract,
)

_HN_MOD = "app.collection.fetchers.hacker_news"


def _hit(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "objectID": "47139675",
        "title": "I'm helping my dog vibe code games",
        "url": "https://www.calebleak.com/posts/dog-game/",
        "author": "cleak",
        "points": 1082,
        "num_comments": 365,
        "story_text": None,
        "created_at": "2026-02-24T17:15:17Z",
        "created_at_i": 1771953317,
        "_tags": ["story", "author_cleak", "story_47139675"],
    }
    base.update(overrides)
    return base


_SAMPLE_HITS = [
    _hit(),
    _hit(objectID="47140000", url=None, title="Ask HN: text post"),
    _hit(
        objectID="47140001",
        title="New Rust release v2.0",
        url="https://blog.rust-lang.org/2026/02/24/rust-2.html",
        author="rustdev",
    ),
]


def _mock_response(data: dict[str, Any], status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        text=json.dumps(data),
        headers={"content-type": "application/json"},
        request=httpx.Request("GET", "https://hn.algolia.com/api/v1/search_by_date"),
    )


def _mock_safe_client(response_or_exception: Any) -> MagicMock:
    client = AsyncMock()
    if isinstance(response_or_exception, BaseException):
        client.get = AsyncMock(side_effect=response_or_exception)
    else:
        client.get = AsyncMock(return_value=response_or_exception)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


async def _collect(it: AsyncIterator[FetchOutcome]) -> list[FetchOutcome]:
    return [o async for o in it]


def _direct_outcomes() -> list[FetchOutcome]:
    fetcher = HackerNewsFetcher()
    return [
        outcome
        for hit in _SAMPLE_HITS
        if (outcome := fetcher._convert_hit(hit, 1)) is not None
    ]


def test_passports_satisfy_persistence_invariants() -> None:
    assert_passports_persistable(_direct_outcomes())


def test_provides_contract_holds() -> None:
    assert_provides_contract(_direct_outcomes(), HackerNewsFetcher.PROVIDES)


def test_metadata_audit_safe() -> None:
    assert_metadata_audit_safe(_direct_outcomes())


def test_url_missing_hit_skipped_silently() -> None:
    """Ask HN 系 (url 欠落) は yield 自体せず skip。``SourceFetchFailed`` も流さない。"""  # noqa: E501
    fetcher = HackerNewsFetcher()
    assert fetcher._convert_hit(_hit(url=None), 1) is None
    assert fetcher._convert_hit(_hit(url=""), 1) is None


def test_invalid_url_returns_failed_not_corrupt_passport() -> None:
    fetcher = HackerNewsFetcher()
    outcome = fetcher._convert_hit(_hit(url="not-a-url"), 1)
    assert isinstance(outcome, SourceFetchFailed)
    assert outcome.reason.code == "extraction_empty"


def test_empty_title_returns_failed() -> None:
    fetcher = HackerNewsFetcher()
    outcome = fetcher._convert_hit(_hit(title=""), 1)
    assert isinstance(outcome, SourceFetchFailed)
    assert outcome.reason.code == "title_missing"


@pytest.mark.asyncio
async def test_fetch_yields_passports_for_valid_hits() -> None:
    cm = _mock_safe_client(_mock_response({"hits": _SAMPLE_HITS}))
    with patch(f"{_HN_MOD}.make_safe_async_client", return_value=cm):
        outcomes = await _collect(HackerNewsFetcher().fetch(1))
    assert_at_least_one_passport(outcomes)
    # url 欠落 1 件は skip され、残り 2 件が IncompleteArticle
    pendings = [
        o
        for o in outcomes
        if isinstance(o, FetchedEntry) and isinstance(o.item, IncompleteArticle)
    ]
    assert len(pendings) == 2


@pytest.mark.asyncio
async def test_fetch_403_classified_as_permanent() -> None:
    response = _mock_response({}, status_code=403)
    error = httpx.HTTPStatusError("403", request=response.request, response=response)
    cm = _mock_safe_client(error)
    with (
        patch(f"{_HN_MOD}.make_safe_async_client", return_value=cm),
        pytest.raises(PermanentFetchError),
    ):
        await _collect(HackerNewsFetcher().fetch(1))


@pytest.mark.asyncio
async def test_fetch_5xx_classified_as_temporary() -> None:
    response = _mock_response({}, status_code=500)
    error = httpx.HTTPStatusError("500", request=response.request, response=response)
    cm = _mock_safe_client(error)
    with (
        patch(f"{_HN_MOD}.make_safe_async_client", return_value=cm),
        pytest.raises(TemporaryFetchError),
    ):
        await _collect(HackerNewsFetcher().fetch(1))


@pytest.mark.asyncio
async def test_request_params_carry_quality_filters() -> None:
    """API request に sliding window / min points / story tag が必ず載る。"""
    cm = _mock_safe_client(_mock_response({"hits": []}))
    fixed_now = 1_800_000_000
    with (
        patch(f"{_HN_MOD}.make_safe_async_client", return_value=cm),
        patch(f"{_HN_MOD}.time.time", return_value=fixed_now),
    ):
        await _collect(HackerNewsFetcher().fetch(1))
    inner: AsyncMock = cm.__aenter__.return_value
    params = inner.get.call_args.kwargs.get("params", {})
    nf = params.get("numericFilters", "")
    assert f"created_at_i>{fixed_now - HN_SLIDING_WINDOW_SECONDS}" in nf
    assert f"points>{HN_MIN_POINTS}" in nf
    assert params["hitsPerPage"] == HN_HITS_PER_PAGE
    assert params["tags"] == "story"
