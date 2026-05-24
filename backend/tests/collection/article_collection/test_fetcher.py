"""``fetch_articles`` engine の合成不変条件テスト (I/O 非依存)。

engine は Source 宣言を **read → in_scope filter → select → map_entry** の順に
合成する。stub source で (1) 合成順序、(2) ``BaseArticleSource`` default の供給、
(3) ``read`` の ``ExternalFetchError`` 素通りを固定する。
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from app.collection.article_collection.fetched_article import FetchedArticle
from app.collection.article_collection.fetcher import fetch_articles
from app.collection.article_collection.tools.reader_tools import ReaderTools
from app.collection.external_fetch_errors import FetchOriginServerError
from app.collection.sources.base_article_source import BaseArticleSource

_TOOLS = ReaderTools()


def _fa(title: str) -> FetchedArticle:
    return FetchedArticle(
        title=title, url=f"https://e.test/{title}", body=None, published_at=None
    )


async def _drain(source: object) -> list[FetchedArticle]:
    return [fa async for fa in fetch_articles(source, _TOOLS)]  # type: ignore[arg-type]


class _OrderSource(BaseArticleSource):
    """in_scope が ``"X"`` を除外、select が逆順化、map_entry が写像する観測 stub。"""

    received_by_select: ClassVar[list[str]] = []

    @classmethod
    async def read(cls, tools: ReaderTools) -> list[str]:  # noqa: ARG003
        return ["a", "X", "b"]

    @classmethod
    def in_scope(cls, entry: str) -> bool:
        return entry != "X"

    @classmethod
    def select(cls, entries: list[str]) -> list[str]:
        cls.received_by_select = list(entries)
        return list(reversed(entries))

    @classmethod
    def map_entry(cls, entry: str) -> FetchedArticle:
        return _fa(entry)


async def test_pipeline_applies_scope_then_select_then_map_in_order() -> None:
    out = await _drain(_OrderSource)

    # in_scope が "X" を除外 → select が受けるのは post-filter の ["a", "b"]。
    assert _OrderSource.received_by_select == ["a", "b"]
    # select が逆順化 → map_entry 適用後の順序は ["b", "a"]。
    assert [fa.title for fa in out] == ["b", "a"]


class _DefaultsSource(BaseArticleSource):
    """``read`` + ``map_entry`` のみ定義 (in_scope/select は Base default)。"""

    @classmethod
    async def read(cls, tools: ReaderTools) -> list[str]:  # noqa: ARG003
        return ["x", "y"]

    @classmethod
    def map_entry(cls, entry: str) -> FetchedArticle:
        return _fa(entry)


async def test_base_defaults_supply_in_scope_and_select() -> None:
    """Base default (in_scope=全件 True / select=恒等) で read 全件が写像される。

    Protocol は default body を非継承 class に供給できないため、Base mixin が
    供給する。欠落すると engine が ``AttributeError`` で落ちる回帰検知。
    """
    out = await _drain(_DefaultsSource)

    assert [fa.title for fa in out] == ["x", "y"]


class _FailingReadSource(BaseArticleSource):
    """``read`` が ``ExternalFetchError`` を raise する stub。"""

    @classmethod
    async def read(cls, tools: ReaderTools) -> list[str]:  # noqa: ARG003
        raise FetchOriginServerError(status_code=503, reason="boom")

    @classmethod
    def map_entry(cls, entry: str) -> FetchedArticle:
        return _fa(entry)


async def test_read_external_fetch_error_passes_through() -> None:
    """``read`` の ``ExternalFetchError`` を engine は握りつぶさず素通りさせる。"""
    with pytest.raises(FetchOriginServerError):
        await _drain(_FailingReadSource)
