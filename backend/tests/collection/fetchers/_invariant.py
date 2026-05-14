"""Fetcher invariant 共通アサーション。

各 Fetcher の per-source テストは「実 RSS / sitemap fixture を流したとき
ビジネスロジックの不変条件が守られるか」だけを検証する。具体的な author
名や tags の中身など、ソース次第で変動する値の枚挙は書かない
(memory `feedback_test_invariants_over_change_tracking.md`)。

Outcome 純化原則 (PR-2 以降): Fetcher が yield するのは
``ReadyForArticle | IncompleteArticle`` の passport のみ。品質ゲート未達 entry
は yield しないため、観測点は「yield された passport」だけになる。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from app.collection.article.domain.article import ReadyForArticle
from app.collection.article.domain.value_objects import PublishedAt
from app.collection.incomplete_article.domain.incomplete_article import (
    IncompleteArticle,
)

_DEFAULT_HTML_PUBLISHED_AT = PublishedAt(value=datetime(2026, 5, 1, tzinfo=UTC))

Passport = ReadyForArticle | IncompleteArticle


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

    Pattern R: ``ReadyForArticle`` の Pydantic 構築が成功している = 5 fields 通過済。
    Pattern H: ``IncompleteArticle`` + HTML 抽出値で ``complete_with_html`` が
    ``ReadyForArticle`` を返せること (= Stage 2 を通せば永続化できる中間状態)。

    ``html_published_at`` 省略時は default を入れる (Pattern H が published_at を
    HTML 側から確定させる前提を表現するため)。
    """
    pub = html_published_at or _DEFAULT_HTML_PUBLISHED_AT
    for item in items:
        if isinstance(item, ReadyForArticle):
            continue
        assert isinstance(item, IncompleteArticle)
        promoted = item.complete_with_html(
            body=html_body,
            html_published_at=pub,
        )
        assert isinstance(promoted, ReadyForArticle), (
            f"IncompleteArticle could not be promoted to ReadyForArticle: {promoted}"
        )
