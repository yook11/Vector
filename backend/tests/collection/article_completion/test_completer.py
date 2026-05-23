"""``ArticleHtmlCompleter`` の契約テスト — 純粋境界の出力型保証。

検証する不変条件 (副作用ゼロ。DB を触らず ``acquirer.acquire`` のみ差し替え、
戻り値が ``AnalyzableArticle | CompletionFailure`` の閉じ union に必ず収まる):

- fetch 例外 (``ExternalFetchError``) → ``FetchFailed`` 値に畳まれ例外は出ない
- ``AcquisitionFailure`` → 抽出結果が無く完成できないので取得層の判定を surface
- ``AcquiredContent`` + promotion 成功 → ``AnalyzableArticle``
- ``AcquiredContent`` + promotion 失敗 → ``CompletionInvariantRejected``

completer は profile を知らず ``ReadyForArticleCompletion`` 経由で受け取り
``complete_with_html`` (純粋関数) に委譲する。
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from app.collection.article_completion.acquirer import AcquiredContent
from app.collection.article_completion.acquisition_failure import NotHtml
from app.collection.article_completion.completer import (
    ArticleHtmlCompleter,
    FetchFailed,
)
from app.collection.article_completion.completion_failure import (
    CompletionInvariantRejected,
)
from app.collection.article_completion.ready import ReadyForArticleCompletion
from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.canonical_article_url import CanonicalArticleUrl
from app.collection.domain.observed_article import (
    ObservedArticle,
    ObservedField,
    ObservedOrigin,
)
from app.collection.domain.value_objects import PublishedAt
from app.collection.external_fetch_errors import FetchResourceNotFoundError
from app.collection.sources.article_completion_policy import DEFAULT_POLICY
from app.shared.value_objects.source_name import SourceName

_URL = CanonicalArticleUrl("https://example.com/article")


def _completer(acquire: AsyncMock) -> ArticleHtmlCompleter:
    """``acquirer.acquire`` を差し替えた completer を返す (副作用なし)。"""
    acquirer = AsyncMock()
    acquirer.acquire = acquire
    return ArticleHtmlCompleter(acquirer_factory=lambda: acquirer)


def _ready(
    *,
    observed_published: PublishedAt | None = PublishedAt(
        value=datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC)
    ),
) -> ReadyForArticleCompletion:
    return ReadyForArticleCompletion(
        pending_id=1,
        source_id=1,
        attempt_count=1,
        observed=ObservedArticle(
            source_name=SourceName("Feed Source"),
            source_url=_URL,
            title=ObservedField(value="Feed Title", origin=ObservedOrigin.feed),
            published_at=(
                ObservedField(value=observed_published, origin=ObservedOrigin.feed)
                if observed_published is not None
                else None
            ),
        ),
        profile=DEFAULT_POLICY,
        source_url=_URL,
    )


@pytest.mark.asyncio
async def test_fetch_error_is_folded_into_fetch_failed_value() -> None:
    """fetch の ``ExternalFetchError`` は例外でなく ``FetchFailed`` 値で返る。"""
    err = FetchResourceNotFoundError(status_code=404, reason="not_found")
    result = await _completer(AsyncMock(side_effect=err)).complete(_ready())

    assert result == FetchFailed(error=err)


@pytest.mark.asyncio
async def test_acquisition_failure_passes_through_as_value() -> None:
    """``AcquisitionFailure`` は抽出結果が無く完成不能なので取得層の判定を surface。"""
    failure = NotHtml(content_type="application/pdf")
    result = await _completer(AsyncMock(return_value=failure)).complete(_ready())

    assert result is failure


@pytest.mark.asyncio
async def test_acquired_content_success_returns_analyzable_article() -> None:
    """``AcquiredContent`` + promotion 成功 → ``AnalyzableArticle``。"""
    content = AcquiredContent(
        title="HTML Title",
        body="x" * 200,
        published_at=PublishedAt(value=datetime(2026, 5, 1, tzinfo=UTC)),
    )
    result = await _completer(AsyncMock(return_value=content)).complete(_ready())

    assert isinstance(result, AnalyzableArticle)


@pytest.mark.asyncio
async def test_promotion_failure_returns_invariant_rejected() -> None:
    """published_at が観測 / HTML 両方欠落 → 必須 Field 違反として
    ``CompletionInvariantRejected`` に畳む。"""
    content = AcquiredContent(title="OK", body="x" * 200, published_at=None)
    result = await _completer(AsyncMock(return_value=content)).complete(
        _ready(observed_published=None)
    )

    assert isinstance(result, CompletionInvariantRejected)
    assert result.error_class == "ValidationError"
    assert "published_at" in result.error_message
