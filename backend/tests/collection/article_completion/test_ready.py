"""ReadyForArticleCompletion のドメインユニットテスト。"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from app.audit.domain.event import EventType
from app.collection.article_completion.ready import (
    ArticleCompletionReadyBuildFacts,
    ArticleCompletionReadyBuildPendingMissingError,
    ArticleCompletionReadyBuildPendingNotRunningError,
    ReadyForArticleCompletion,
)
from app.collection.domain.canonical_article_url import (
    CanonicalArticleUrl,
    CanonicalArticleUrlInvalidError,
)
from app.collection.domain.observed_article import (
    ObservedArticle,
    ObservedArticleInvalidError,
    ObservedField,
    ObservedOrigin,
)
from app.collection.domain.value_objects import PublishedAt
from app.collection.sources.article_completion_policy import (
    HTML_TITLE_POLICY,
)
from app.collection.sources.errors import SourceNotRegisteredError
from app.collection.sources.source_name import SourceName
from app.shared.security.safe_url import SafeUrlInvalidReason


def _observed_article(
    *,
    source_name: SourceName = SourceName("TechCrunch"),
    source_url: str = "https://example.com/a",
    title: str = "Title",
) -> dict:
    observed = ObservedArticle(
        source_name=source_name,
        source_url=CanonicalArticleUrl(source_url),
        title=ObservedField(value=title, origin=ObservedOrigin.feed),
        published_at=ObservedField(
            value=PublishedAt(datetime(2026, 5, 1, tzinfo=UTC)),
            origin=ObservedOrigin.feed,
        ),
    )
    return observed.model_dump(mode="json", by_alias=True)


def _facts(
    *,
    pending_id: int = 42,
    source_name: SourceName = SourceName("TechCrunch"),
    status: str = "running",
    source_url: str = "https://example.com/a",
    observed_article: dict | None = None,
    attempt_count: int = 1,
) -> ArticleCompletionReadyBuildFacts:
    return ArticleCompletionReadyBuildFacts(
        pending_id=pending_id,
        source_id=7,
        source_name=source_name,
        status=status,
        observed_article=(
            observed_article
            if observed_article is not None
            else _observed_article(source_name=source_name, source_url=source_url)
        ),
        source_url=source_url,
        attempt_count=attempt_count,
    )


def _repo_mock(
    facts: ArticleCompletionReadyBuildFacts | None,
) -> AsyncMock:
    repo = AsyncMock()
    repo.load_ready_build_facts = AsyncMock(return_value=facts)
    return repo


@pytest.mark.asyncio
async def test_builds_ready_from_repository_facts() -> None:
    source_name = SourceName("Anthropic")
    facts = _facts(source_name=source_name)
    repo = _repo_mock(facts)

    ready = await ReadyForArticleCompletion.try_advance_from(
        pending_id=42,
        repo=repo,
    )

    assert ready.pending_id == 42
    assert ready.source_id == 7
    assert ready.attempt_count == 1
    assert ready.source_url == CanonicalArticleUrl("https://example.com/a")
    assert ready.profile is HTML_TITLE_POLICY
    assert ready.observed.source_name == source_name
    assert ready.observed.title is not None
    assert ready.observed.title.value == "Title"
    repo.load_ready_build_facts.assert_awaited_once_with(42)


@pytest.mark.asyncio
async def test_raises_skipped_error_when_pending_missing() -> None:
    repo = _repo_mock(None)

    with pytest.raises(ArticleCompletionReadyBuildPendingMissingError) as exc_info:
        await ReadyForArticleCompletion.try_advance_from(
            pending_id=999,
            repo=repo,
        )

    assert exc_info.value.EVENT_TYPE is EventType.SKIPPED
    assert exc_info.value.CODE == "completion_ready_build_blocked_pending_missing"
    repo.load_ready_build_facts.assert_awaited_once_with(999)


@pytest.mark.asyncio
async def test_raises_skipped_error_when_pending_not_running() -> None:
    facts = _facts(status="open")
    repo = _repo_mock(facts)

    with pytest.raises(ArticleCompletionReadyBuildPendingNotRunningError) as exc_info:
        await ReadyForArticleCompletion.try_advance_from(
            pending_id=42,
            repo=repo,
        )

    assert exc_info.value.EVENT_TYPE is EventType.SKIPPED
    assert exc_info.value.CODE == "completion_ready_build_blocked_pending_not_running"


@pytest.mark.asyncio
async def test_raises_failed_when_observed_article_invalid() -> None:
    facts = _facts(observed_article={"title": {"value": "Title", "origin": "bad"}})

    with pytest.raises(ObservedArticleInvalidError):
        await ReadyForArticleCompletion.try_advance_from(
            pending_id=42,
            repo=_repo_mock(facts),
        )


@pytest.mark.asyncio
async def test_raises_failed_when_source_not_registered() -> None:
    facts = _facts(source_name=SourceName("Definitely Missing Source"))

    with pytest.raises(SourceNotRegisteredError):
        await ReadyForArticleCompletion.try_advance_from(
            pending_id=42,
            repo=_repo_mock(facts),
        )


@pytest.mark.asyncio
async def test_raises_failed_when_url_invalid() -> None:
    facts = _facts(source_url="ftp://example.com/a", observed_article={})

    with pytest.raises(CanonicalArticleUrlInvalidError) as exc_info:
        await ReadyForArticleCompletion.try_advance_from(
            pending_id=42,
            repo=_repo_mock(facts),
        )

    # ready の URL 失敗は VO 例外で reason を運ぶ (where/code は audit の責務)
    assert exc_info.value.reason is SafeUrlInvalidReason.URL_NOT_HTTP
