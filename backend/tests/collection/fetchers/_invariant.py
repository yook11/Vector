"""Fetcher invariant 共通アサーション。

各 Fetcher の per-source テストは「実 RSS / sitemap fixture を流したとき
ビジネスロジックの不変条件が守られるか」だけを検証する。具体的な author
名や tags の中身など、ソース次第で変動する値の枚挙は書かない
(memory `feedback_test_invariants_over_change_tracking.md`)。

Outcome 純化原則 (PR-2 以降): Fetcher が yield するのは
``AnalyzableArticle | IncompleteArticle`` の passport のみ。品質ゲート未達 entry
は yield しないため、観測点は「yield された passport」だけになる。

passport builder への切替 (本 PR) 以降は同じ Fetcher でも entry ごとに
Ready / Incomplete を選びうるため、type 集合の検証を 2 段階で行う:

- ``assert_passport_types_allowed`` — 全 passport が ``allowed`` 集合に属する
  (= 想定外の型が混ざっていないこと)
- ``assert_passport_types_include`` — ``must_include`` の各型を最低 1 件含む
  (= 主経路 / 副経路が壊れていないこと)
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.incomplete_article import (
    IncompleteArticle,
)
from app.collection.domain.value_objects import PublishedAt

_DEFAULT_HTML_PUBLISHED_AT = PublishedAt(value=datetime(2026, 5, 1, tzinfo=UTC))

Passport = AnalyzableArticle | IncompleteArticle


def assert_at_least_one_passport(items: Iterable[Passport]) -> None:
    """全 entry が drop = ソース壊滅。fixture で検知する。"""
    materialized = list(items)
    assert materialized, (
        "fetcher produced no passport from fixture; pipeline cannot proceed"
    )


def assert_passports_persistable(
    items: Iterable[Passport],
    *,
    html_body: str = "x" * 200,
    html_published_at: PublishedAt | None = None,
) -> None:
    """全 passport が永続化不変条件を満たすこと。

    AnalyzableArticle: Pydantic 構築が成功している = 5 fields 通過済。
    IncompleteArticle: HTML 抽出値で ``complete_with_html`` が AnalyzableArticle
    を返せる (= Stage 2 を通せば永続化できる中間状態)。

    ``html_published_at`` 省略時は default を入れる (Pattern H が published_at を
    HTML 側から確定させる前提を表現するため)。
    """
    pub = html_published_at or _DEFAULT_HTML_PUBLISHED_AT
    for item in items:
        if isinstance(item, AnalyzableArticle):
            continue
        assert isinstance(item, IncompleteArticle)
        promoted = item.complete_with_html(
            body=html_body,
            html_published_at=pub,
        )
        assert isinstance(promoted, AnalyzableArticle), (
            f"IncompleteArticle could not be promoted to AnalyzableArticle: {promoted}"
        )


def assert_passport_types_allowed(
    items: Iterable[Passport],
    *,
    allowed: set[type],
) -> None:
    """全 passport の型が ``allowed`` 集合に属することを保証する。

    例: Pattern H 固定ソース (``body_candidate=None``) に対し
    ``allowed={IncompleteArticle}`` を渡せば、Ready が混入していないことを固定。
    """
    materialized = list(items)
    actual_types = {type(item) for item in materialized}
    unexpected = actual_types - allowed
    assert not unexpected, (
        f"passport types {unexpected} not in allowed set {allowed}; "
        f"materialized={[type(i).__name__ for i in materialized]}"
    )


def assert_passport_types_include(
    items: Iterable[Passport],
    *,
    must_include: set[type],
) -> None:
    """``must_include`` の各型を最低 1 件含むことを保証する。

    fallback で型が混じる可能性は ``allowed`` 側で許容しつつ、主経路の型が
    最低 1 件 yield されることを固定する用途。
    """
    materialized = list(items)
    actual_types = {type(item) for item in materialized}
    missing = must_include - actual_types
    assert not missing, (
        f"passport types {missing} were expected but not produced; "
        f"materialized={[type(i).__name__ for i in materialized]}"
    )
