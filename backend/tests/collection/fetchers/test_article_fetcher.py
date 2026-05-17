"""``ArticleFetcher`` のユニットテスト (DB / HTTP 非依存)。

P2 で ``ArticleFetcher`` は ``ArticleSource`` 集約を受け、``source.make_adapter()``
で取得 machinery を **毎 fetch 構築** する。fake machinery を
``adapter_factory`` に注入し、Source → machinery → builder → passport の配線が
薄い層として正しく動くことを検証する。machinery 単体の挙動 (RSS parse や
filter) は per-source テストの責務、本テストは "machinery が yield する
FetchedArticle を ArticleFetcher が正しく中継する" + "fetch 毎に
make_adapter が呼ばれる" の 2 点に絞る。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.article_limits import ARTICLE_BODY_MIN_LENGTH
from app.collection.domain.observed_article import ObservedArticle, ObservedOrigin
from app.collection.domain.source_completion_profile import DEFAULT_PROFILE
from app.collection.fetchers.article_fetcher import ArticleFetcher
from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.sources.article_source import ArticleSource
from app.shared.value_objects.source_name import SourceName

_PUBLISHED = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
_VALID_URL = "https://example.com/articles/hello-world"
_VALID_TITLE = "Hello World"
_VALID_BODY = "x" * ARTICLE_BODY_MIN_LENGTH


class _FakeAdapter:
    """slim ``SourceAdapter`` (collect() のみ)。構築回数を記録する。"""

    def __init__(self, items: list[FetchedArticle]) -> None:
        self._items = items

    async def collect(self) -> AsyncIterator[FetchedArticle]:
        for item in self._items:
            yield item


def _source(items: list[FetchedArticle], *, build_calls: list[int]) -> ArticleSource:
    """``_FakeAdapter`` を factory でラップした ``ArticleSource``。

    ``build_calls`` に factory 呼出を記録し、fetch 毎 machinery 構築を観測する。
    """

    def _factory() -> _FakeAdapter:
        build_calls.append(1)
        return _FakeAdapter(items)

    return ArticleSource(
        name=SourceName("Fake"),
        endpoint_url="https://example.test/feed",
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=_factory,
    )


async def _collect_fetch(fetcher: ArticleFetcher, source_id: int) -> list:
    return [item async for item in fetcher.fetch(source_id)]


async def test_yields_ready_when_adapter_emits_valid_article() -> None:
    source = _source(
        [
            FetchedArticle(
                title=_VALID_TITLE,
                url=_VALID_URL,
                body=_VALID_BODY,
                published_at=_PUBLISHED,
            )
        ],
        build_calls=[],
    )
    fetcher = ArticleFetcher(source)

    results = await _collect_fetch(fetcher, source_id=1)

    assert len(results) == 1
    assert isinstance(results[0], AnalyzableArticle)


async def test_skips_when_adapter_emits_drop_candidate() -> None:
    """title="" は builder で drop、ArticleFetcher は何も yield しない。"""
    source = _source(
        [
            FetchedArticle(
                title="",
                url=_VALID_URL,
                body=_VALID_BODY,
                published_at=_PUBLISHED,
            ),
            FetchedArticle(
                title=_VALID_TITLE,
                url=_VALID_URL,
                body=None,
                published_at=None,
            ),
        ],
        build_calls=[],
    )
    fetcher = ArticleFetcher(source)

    results = await _collect_fetch(fetcher, source_id=1)

    assert len(results) == 1
    assert isinstance(results[0], ObservedArticle)


def test_exposes_source_name_and_endpoint_url_as_instance_attrs() -> None:
    source = _source([], build_calls=[])
    fetcher = ArticleFetcher(source)

    assert fetcher.NAME == "Fake"
    assert fetcher.ENDPOINT_URL == "https://example.test/feed"


async def test_make_adapter_is_called_once_per_fetch() -> None:
    """machinery は ``fetch`` 毎に新規構築される (旧 ``A()`` 毎回 new の意味保存)。"""
    build_calls: list[int] = []
    source = _source([], build_calls=build_calls)
    fetcher = ArticleFetcher(source)

    assert build_calls == []
    await _collect_fetch(fetcher, source_id=1)
    assert len(build_calls) == 1
    await _collect_fetch(fetcher, source_id=1)
    assert len(build_calls) == 2
