"""``HackerNewsSource`` (Algolia Search API, Pattern H) の不変条件テスト (P2-D)。

このファイルが固定するのは HN Source 固有で他に被覆の無い不変条件:

- ``search_recent_stories`` に renamed kwargs (sliding window / min_points /
  hits_per_page) が必ず渡る (旧仕様: 24h window / points>20 / 100 hits)
- ``map_entry`` が url 欠落 hit を握りつぶさず **total** に
  ``FetchedArticle(url="")`` を出し、収集 → 変換経路で ``ConversionRejection``
  として可視化される (spec「写像で None/drop/skip しない」は写像ごとに
  pin が要る — converter テストは ``FetchedArticle`` を直接与え HN
  写像を通らないため、ここでしか HN シームの totality を pin できない。
  HN は収集スコープ述語を持たず全 entry を写すため degenerate witness は
  単に url=None entry)
- ``HackerNewsReader`` の ``ExternalFetchError`` は ``collect`` を素通しする

passport 業務不変条件は ``test_non_rss_adapters_invariants.py`` [HackerNews]
が 系統A シートベルトとして所有。degenerate hit の棄却 *理由* (MISSING_URL
等) は converter 層 (``test_fetched_article_converter.py``) が機構非依存 SSoT
として所有し、本ファイルは理由を再検証せず「HN 写像が total で可視化に到達
する」リンクのみ pin する。旧 ``test_*_skipped_in_collect`` / ``count==4`` は
spec が意図的に壊す silent-drop を業務ルールとして凍結する確認重複だったため
削除した。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.collection.article_collection.fetched_article import FetchedArticle
from app.collection.article_collection.fetched_article_converter import (
    ConversionRejection,
)
from app.collection.article_collection.reader.algolia_hn_reader import (
    HackerNewsEntry,
    HackerNewsReader,
    normalize_hit,
)
from app.collection.domain.observed_article import ObservedArticle
from app.collection.external_fetch_errors import (
    FetchAccessDeniedError,
    FetchOriginServerError,
)
from app.collection.sources.definitions.hacker_news import (
    HN_HITS_PER_PAGE,
    HN_MIN_POINTS,
    HN_SLIDING_WINDOW_SECONDS,
    HackerNewsSource,
)
from tests.collection.sources._fixture_tools import fixture_tools
from tests.collection.sources._invariant import FetchItem, drive_source

_HN_NAME = "Hacker News"


class _FakeHNClient(HackerNewsReader):
    """kwargs spy。呼出 kwargs を記録し hits を本物の ``normalize_hit`` で
    ``HackerNewsEntry`` 列に写す (構造的 fake)。"""

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
    ) -> list[HackerNewsEntry]:
        self.calls.append(
            {
                "source_name": source_name,
                "min_points": min_points,
                "window_seconds": window_seconds,
                "hits_per_page": hits_per_page,
            }
        )
        return [normalize_hit(h) for h in self._hits]


class _RaisingHNClient(HackerNewsReader):
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def search_recent_stories(
        self,
        *,
        source_name: str,  # noqa: ARG002
        min_points: int,  # noqa: ARG002
        window_seconds: int,  # noqa: ARG002
        hits_per_page: int,  # noqa: ARG002
    ) -> list[HackerNewsEntry]:
        raise self._exc


async def _drive(client: HackerNewsReader) -> list[FetchItem]:
    """HN Source を fixture client 注入で収集 → 変換経路に通す (api+DEFAULT)。"""
    return await drive_source(HackerNewsSource, tools=fixture_tools(hacker_news=client))


@pytest.mark.asyncio
async def test_client_kwargs_carry_quality_filters() -> None:
    """Source は旧仕様 (24h window / points>20 / 100 hits) を client に渡す。"""
    fake = _FakeHNClient([])
    await _drive(fake)
    assert fake.calls == [
        {
            "source_name": _HN_NAME,
            "min_points": HN_MIN_POINTS,
            "window_seconds": HN_SLIDING_WINDOW_SECONDS,
            "hits_per_page": HN_HITS_PER_PAGE,
        }
    ]


# ── 写像 totality (spec「写像で None/drop/skip しない」を HN シームで pin) ──


def test_mapping_is_total_on_url_none_hit() -> None:
    """url 欠落 hit に対し写像は None/raise/skip せず ``FetchedArticle(url="")``
    を返す (total)。

    converter/fetcher テストは ``FetchedArticle`` を直接与え HN 写像を
    通らないため、HN シームの totality はここでしか pin できない。
    """
    fa = HackerNewsSource.map_entry(
        HackerNewsEntry(
            url=None, title="Ask HN: x", published=None, raw_created_at=None
        )
    )
    assert isinstance(fa, FetchedArticle)
    assert fa.url == ""  # 握りつぶさず空 URL を素通し (converter が可視化)


@pytest.mark.asyncio
async def test_url_none_hit_surfaces_as_rejection_without_stopping_stream() -> None:
    """url 欠落 hit は黙って消えず ``ConversionRejection`` として現れ、
    他の hit は ``ObservedArticle`` のまま stream が止まらない。

    旧 ``test_url_none_hits_skipped_in_collect`` (``assert items == []``) が
    凍結していた silent-drop の **真の不変条件** (failure-visibility) を
    Red 先行で再建。
    """
    valid = {"url": "https://example.com/a", "title": "valid", "created_at": None}
    no_url = {"title": "Ask HN: no url", "created_at": None}
    items = await _drive(_FakeHNClient([valid, no_url]))
    assert any(isinstance(i, ObservedArticle) for i in items)  # valid 健在
    assert any(isinstance(i, ConversionRejection) for i in items)  # degenerate 可視
    assert len(items) == 2  # stream が止まらず両方到達 (片方 raise で停止しない)


@pytest.mark.asyncio
async def test_non_recoverable_error_propagates_through_collect() -> None:
    client = _RaisingHNClient(
        FetchAccessDeniedError(status_code=403, reason="forbidden")
    )
    with pytest.raises(FetchAccessDeniedError):
        await _drive(client)


@pytest.mark.asyncio
async def test_recoverable_error_propagates_through_collect() -> None:
    client = _RaisingHNClient(
        FetchOriginServerError(status_code=500, reason="internal_error")
    )
    with pytest.raises(FetchOriginServerError):
        await _drive(client)
