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

import pytest

from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.article_limits import ARTICLE_BODY_MIN_LENGTH
from app.collection.domain.canonical_article_url import CanonicalArticleUrl
from app.collection.domain.observed_article import ObservedArticle, ObservedOrigin
from app.collection.source_fetch import article_fetcher as article_fetcher_module
from app.collection.source_fetch.article_fetcher import ArticleFetcher
from app.collection.source_fetch.errors import ConversionReason
from app.collection.source_fetch.fetched_article import FetchedArticle
from app.collection.source_fetch.fetched_article_converter import ConversionRejection
from app.collection.source_fetch.tools.fetch_tools import FetchTools
from app.collection.sources.article_completion_policy import (
    DEFAULT_POLICY,
    ArticleCompletionPolicy,
)
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
        completion_policy: ClassVar[ArticleCompletionPolicy] = DEFAULT_POLICY

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


async def test_yields_rejection_for_unconvertible_entry_without_stopping_stream() -> (
    None
):
    """title="" は変換不能だが握りつぶさず ``ConversionRejection`` を yield し、
    後続 entry の変換は止まらない (stream 継続 = 1 件不良で source 全停止しない)。"""
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

    assert len(results) == 2
    assert isinstance(results[0], ConversionRejection)
    assert results[0].error.conversion_reason is ConversionReason.MISSING_TITLE
    assert isinstance(results[1], ObservedArticle)


def test_exposes_source_name_and_endpoint_url_as_instance_attrs() -> None:
    source = _make_source([], collect_calls=[])
    fetcher = ArticleFetcher(source)

    assert fetcher.NAME == "Fake"
    assert fetcher.ENDPOINT_URL == "https://example.test/feed"


async def test_yields_unexpected_rejection_without_stopping_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``convert_fetched_article`` が想定外 ``Exception`` を raise しても stream は
    止まらず ``UNEXPECTED_ERROR`` の ``ConversionRejection`` に値化される。

    Stage 1 stream 境界の safety net: precondition 通過後の invariant 違反
    (=ありえない筈の bug) が漏れたとき、source 全体 rollback ではなく per-entry
    rejection 化 + stack trace 残存で運用可視化することを固定する。``__cause__``
    に原因例外が連鎖し、後続 entry の変換は継続することを併せて検証する。
    """
    valid_fetched = FetchedArticle(
        title=_VALID_TITLE,
        url=_VALID_URL,
        body=_VALID_BODY,
        published_at=_PUBLISHED,
    )
    source = _make_source([valid_fetched, valid_fetched], collect_calls=[])
    fetcher = ArticleFetcher(source)

    cause = RuntimeError("unexpected post-precondition invariant violation")
    call_count = {"n": 0}

    def _flaky_convert(fetched, *, source, source_id):  # noqa: ANN001, ARG001
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise cause
        return ObservedArticle.build(
            source_name=source.name,
            source_url=CanonicalArticleUrl(fetched.url),
            title=fetched.title,
            body=fetched.body,
            published_at=None,
            origin=source.observed_origin,
        )

    monkeypatch.setattr(
        article_fetcher_module, "convert_fetched_article", _flaky_convert
    )

    results = await _collect_fetch(fetcher, source_id=1)

    assert len(results) == 2
    assert isinstance(results[0], ConversionRejection)
    assert results[0].error.conversion_reason is ConversionReason.UNEXPECTED_ERROR
    assert results[0].error.__cause__ is cause
    assert isinstance(results[1], ObservedArticle)


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
