"""``ArticleFetcher`` のユニットテスト (DB / HTTP 非依存)。

P2-D で ``ArticleFetcher`` は Source クラスオブジェクト (``ArticleSource``
Protocol を満たす) を受け、``source.collect(tools)`` を **fetch 毎** に呼んで
取得 stream を得る。fake Source クラスを渡し、Source → collect → builder →
passport の配線が薄い層として正しく動くことを検証する。collect 本体の挙動
(RSS parse や filter) は per-source テストの責務、本テストは "collect が
yield する FetchedArticle を ArticleFetcher が正しく中継する" + "fetch 毎に
collect が呼ばれる" の 2 点に絞る。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import ClassVar

from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.article_limits import ARTICLE_BODY_MIN_LENGTH
from app.collection.domain.observed_article import ObservedArticle, ObservedOrigin
from app.collection.domain.source_completion_profile import (
    DEFAULT_PROFILE,
    SourceCompletionProfile,
)
from app.collection.fetchers.article_fetcher import ArticleFetcher
from app.collection.fetchers.tools.fetch_tools import FetchTools
from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.sources.article_source import ArticleSource
from app.shared.value_objects.source_name import SourceName

_PUBLISHED = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
_VALID_URL = "https://example.com/articles/hello-world"
_VALID_TITLE = "Hello World"
_VALID_BODY = "x" * ARTICLE_BODY_MIN_LENGTH


def _make_source(
    items: list[FetchedArticle], *, collect_calls: list[int]
) -> ArticleSource:
    """``items`` を yield する fake Source クラスオブジェクトを生成する。

    ``collect_calls`` に ``collect`` 起動を記録し、fetch 毎の collect 呼出を
    観測する。各呼出で新クラスを ``type`` 生成せずクロージャで items を束ねる
    (クラスオブジェクト自体が ``ArticleSource`` Protocol を構造的に満たす)。
    """

    class _FakeSource:
        name: ClassVar[SourceName] = SourceName("Fake")
        endpoint_url: ClassVar[str] = "https://example.test/feed"
        observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
        completion_profile: ClassVar[SourceCompletionProfile] = DEFAULT_PROFILE

        @classmethod
        async def collect(
            cls,
            tools: FetchTools,  # noqa: ARG003
        ) -> AsyncIterator[FetchedArticle]:
            collect_calls.append(1)
            for item in items:
                yield item

    return _FakeSource


async def _collect_fetch(fetcher: ArticleFetcher, source_id: int) -> list:
    return [item async for item in fetcher.fetch(source_id)]


async def test_yields_ready_when_source_emits_valid_article() -> None:
    source = _make_source(
        [
            FetchedArticle(
                title=_VALID_TITLE,
                url=_VALID_URL,
                body=_VALID_BODY,
                published_at=_PUBLISHED,
            )
        ],
        collect_calls=[],
    )
    fetcher = ArticleFetcher(source)

    results = await _collect_fetch(fetcher, source_id=1)

    assert len(results) == 1
    assert isinstance(results[0], AnalyzableArticle)


async def test_skips_when_source_emits_drop_candidate() -> None:
    """title="" は builder で drop、ArticleFetcher は何も yield しない。"""
    source = _make_source(
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
        collect_calls=[],
    )
    fetcher = ArticleFetcher(source)

    results = await _collect_fetch(fetcher, source_id=1)

    assert len(results) == 1
    assert isinstance(results[0], ObservedArticle)


def test_exposes_source_name_and_endpoint_url_as_instance_attrs() -> None:
    source = _make_source([], collect_calls=[])
    fetcher = ArticleFetcher(source)

    assert fetcher.NAME == "Fake"
    assert fetcher.ENDPOINT_URL == "https://example.test/feed"


async def test_collect_is_invoked_once_per_fetch() -> None:
    """``collect`` は ``fetch`` 毎に新規起動される (旧 ``A()`` 毎回 new の意味保存)。"""
    collect_calls: list[int] = []
    source = _make_source([], collect_calls=collect_calls)
    fetcher = ArticleFetcher(source)

    assert collect_calls == []
    await _collect_fetch(fetcher, source_id=1)
    assert len(collect_calls) == 1
    await _collect_fetch(fetcher, source_id=1)
    assert len(collect_calls) == 2
