"""Fetcher invariant 共通アサーション。

各 Fetcher の per-source テストは「実 RSS / sitemap fixture を流したとき
ビジネスロジックの不変条件が守られるか」だけを検証する。具体的な author
名や tags の中身など、ソース次第で変動する値の枚挙は書かない
(memory `feedback_test_invariants_over_change_tracking.md`)。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime

from app.collection.article.domain.article import ReadyForArticle
from app.collection.article.domain.value_objects import PublishedAt
from app.collection.fetchers.outcome import FetchedEntry, FetchOutcome
from app.collection.incomplete_article.domain.incomplete_article import (
    IncompleteArticle,
)

_DEFAULT_HTML_PUBLISHED_AT = PublishedAt(value=datetime(2026, 5, 1, tzinfo=UTC))


def passports(outcomes: Iterable[FetchOutcome]) -> list[FetchedEntry]:
    return [o for o in outcomes if isinstance(o, FetchedEntry)]


def assert_at_least_one_passport(outcomes: Iterable[FetchOutcome]) -> None:
    """全 entry が ``SourceFetchFailed`` = ソース壊滅。fixture で検知する。"""
    assert passports(outcomes), (
        "fetcher produced no FetchedEntry from fixture; pipeline cannot proceed"
    )


def assert_passports_persistable(
    outcomes: Iterable[FetchOutcome],
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
    for entry in passports(outcomes):
        if isinstance(entry.item, ReadyForArticle):
            continue
        assert isinstance(entry.item, IncompleteArticle)
        promoted = entry.item.complete_with_html(
            body=html_body,
            html_published_at=pub,
        )
        assert isinstance(promoted, ReadyForArticle), (
            f"IncompleteArticle could not be promoted to ReadyForArticle: {promoted}"
        )


def assert_provides_contract(
    outcomes: Iterable[FetchOutcome],
    provides: frozenset[str],
) -> None:
    """``Fetcher.PROVIDES`` に列挙された key は全 entry の metadata に必ず入る。"""
    for entry in passports(outcomes):
        missing = provides - entry.metadata.keys()
        assert not missing, (
            f"PROVIDES contract violation: {missing} missing from "
            f"metadata={dict(entry.metadata)}"
        )


def assert_metadata_audit_safe(outcomes: Iterable[FetchOutcome]) -> None:
    """metadata は ``pipeline_events.payload`` (JSONB) に焼ける primitive のみ。"""
    for entry in passports(outcomes):
        try:
            json.dumps(dict(entry.metadata))
        except TypeError as e:
            raise AssertionError(
                f"metadata is not JSON-serializable: {dict(entry.metadata)}"
            ) from e
