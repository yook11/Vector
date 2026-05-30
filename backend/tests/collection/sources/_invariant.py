"""Fetcher invariant 共通アサーション。

各 Fetcher の per-source テストは「実 RSS / sitemap fixture を流したとき
ビジネスロジックの不変条件が守られるか」だけを検証する。具体的な author
名や tags の中身など、ソース次第で変動する値の枚挙は書かない
(memory `feedback_test_invariants_over_change_tracking.md`)。

Fetcher が yield するのは ``AnalyzableArticle | ObservedArticle`` の passport
と、変換不能 entry の ``AcquisitionConversionRejection`` 値。per-source 不変条件は
「変換成功した passport」の業務性質を固定する関心なので、本 helper は
``AcquisitionConversionRejection`` を ``passports_only`` で分離してから assert する
(棄却の値化 / 監査自体の検証は converter / fetcher / service テストの責務)。

passport builder への切替以降は同じ Fetcher でも entry ごとに
Ready / Observed を選びうるため、type 集合の検証を 2 段階で行う:

- ``assert_passport_types_allowed`` — 全 passport が ``allowed`` 集合に属する
  (= 想定外の型が混ざっていないこと)
- ``assert_passport_types_include`` — ``must_include`` の各型を最低 1 件含む
  (= 主経路 / 副経路が壊れていないこと)

``ObservedArticle`` の永続化不変条件は free function ``complete_with_html``
(profile 駆動) で固定する: ``DEFAULT_POLICY`` + HTML 取得値で
``AnalyzableArticle`` に昇格できる = Stage 2 を通せば articles に焼ける、
という業務不変条件。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from app.collection.article_acquisition.fetched_article_converter import (
    AcquisitionConversionRejection,
    convert_fetched_article,
)
from app.collection.article_acquisition.fetcher import fetch_articles
from app.collection.article_acquisition.tools.reader_tools import ReaderTools
from app.collection.article_completion.completer import complete_with_html
from app.collection.article_completion.scraper import ScrapedContent
from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.observed_article import ObservedArticle
from app.collection.domain.value_objects import PublishedAt
from app.collection.sources.article_completion_policy import DEFAULT_POLICY
from app.collection.sources.article_source import ArticleSource

_DEFAULT_HTML_PUBLISHED_AT = PublishedAt(value=datetime(2026, 5, 1, tzinfo=UTC))

Passport = AnalyzableArticle | ObservedArticle
FetchItem = AnalyzableArticle | ObservedArticle | AcquisitionConversionRejection


async def drive_source(
    source: ArticleSource,
    *,
    tools: ReaderTools,
    source_id: int = 1,
) -> list[FetchItem]:
    """本番経路 (収集 → 変換) を駆動して「何ができたか」の列を返す test harness。

    旧 ``ArticleFetcher`` を置換する。``convert_fetched_article`` が total 化
    したため、per-source テストは ``fetch_articles`` engine の各 ``FetchedArticle``
    を本物の converter に通すだけで passport / 棄却の列が得られる (想定外 bug の
    値化は service の責務なので harness は素通しする)。
    """
    return [
        convert_fetched_article(fetched, source=source, source_id=source_id)
        async for fetched in fetch_articles(source, tools)
    ]


def passports_only(items: Iterable[FetchItem]) -> list[Passport]:
    """fetch stream から ``AcquisitionConversionRejection`` を分離し passport のみ返す。

    per-source 不変条件は変換成功分の業務性質を固定する関心。棄却の値化 /
    監査の検証は converter / fetcher / service テストが担うため、ここでは
    分離するだけ。
    """
    return [
        item for item in items if not isinstance(item, AcquisitionConversionRejection)
    ]


def assert_at_least_one_passport(items: Iterable[FetchItem]) -> None:
    """全 entry が変換不能 = ソース壊滅。fixture で検知する。"""
    materialized = passports_only(items)
    assert materialized, (
        "fetcher produced no passport from fixture; pipeline cannot proceed"
    )


def assert_passports_persistable(
    items: Iterable[FetchItem],
    *,
    html_body: str = "x" * 200,
    html_published_at: PublishedAt | None = None,
) -> None:
    """全 passport が永続化不変条件を満たすこと。

    AnalyzableArticle: Pydantic 構築が成功している = 5 fields 通過済。
    ObservedArticle: HTML 取得値で ``complete_with_html`` (``DEFAULT_POLICY``)
    が AnalyzableArticle を返せる (= Stage 2 を通せば永続化できる中間状態)。

    ``html_published_at`` 省略時は default を入れる (Pattern H が published_at を
    HTML 側から確定させる前提を表現するため)。
    """
    pub = html_published_at or _DEFAULT_HTML_PUBLISHED_AT
    for item in passports_only(items):
        if isinstance(item, AnalyzableArticle):
            continue
        assert isinstance(item, ObservedArticle)
        promoted = complete_with_html(
            item,
            DEFAULT_POLICY,
            ScrapedContent(
                title="HTML Title",
                body=html_body,
                published_at=pub,
            ),
            source_id=1,
            source_url=item.source_url,
        )
        assert isinstance(promoted, AnalyzableArticle), (
            f"ObservedArticle could not be promoted to AnalyzableArticle: {promoted}"
        )


def assert_passport_types_allowed(
    items: Iterable[FetchItem],
    *,
    allowed: set[type],
) -> None:
    """全 passport の型が ``allowed`` 集合に属することを保証する。

    例: Pattern H 固定ソース (``body_candidate=None``) に対し
    ``allowed={ObservedArticle}`` を渡せば、Ready が混入していないことを固定。
    """
    materialized = passports_only(items)
    actual_types = {type(item) for item in materialized}
    unexpected = actual_types - allowed
    assert not unexpected, (
        f"passport types {unexpected} not in allowed set {allowed}; "
        f"materialized={[type(i).__name__ for i in materialized]}"
    )


def assert_passport_types_include(
    items: Iterable[FetchItem],
    *,
    must_include: set[type],
) -> None:
    """``must_include`` の各型を最低 1 件含むことを保証する。

    fallback で型が混じる可能性は ``allowed`` 側で許容しつつ、主経路の型が
    最低 1 件 yield されることを固定する用途。
    """
    materialized = passports_only(items)
    actual_types = {type(item) for item in materialized}
    missing = must_include - actual_types
    assert not missing, (
        f"passport types {missing} were expected but not produced; "
        f"materialized={[type(i).__name__ for i in materialized]}"
    )
