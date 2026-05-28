"""ReadyForCuration (Stage 3 precondition 型) のドメインユニットテスト。"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.analysis.curation.domain.ready import (
    CurationReadyBuildBlockedCode,
    CurationReadyBuildBlockedError,
    CurationReadyBuildFacts,
    ReadyForCuration,
)
from app.queue.messages.curation import CurationTrigger


def _facts(
    *,
    article_id: int = 42,
    title: str = "Quantum Breakthrough",
    content: str = "Article body",
    source_name: str | None = "MIT News",
    has_signal_curation: bool = False,
    has_noise_curation: bool = False,
) -> CurationReadyBuildFacts:
    return CurationReadyBuildFacts(
        article_id=article_id,
        original_title=title,
        original_content=content,
        source_name=source_name,
        has_signal_curation=has_signal_curation,
        has_noise_curation=has_noise_curation,
    )


def _repo_mock(
    *,
    facts: CurationReadyBuildFacts | None = None,
    missing: bool = False,
) -> AsyncMock:
    repo = AsyncMock()
    repo.load_ready_build_facts = AsyncMock(
        return_value=None if missing else facts or _facts()
    )
    return repo


class TestTryAdvanceFrom:
    @pytest.mark.asyncio
    async def test_builds_ready_from_repository_facts(self) -> None:
        facts = _facts(article_id=42, content="Article body" * 10)
        repo = _repo_mock(facts=facts)

        ready = await ReadyForCuration.try_advance_from(article_id=42, repo=repo)

        assert ready == ReadyForCuration(
            article_id=42,
            original_title=facts.original_title,
            original_content=facts.original_content,
        )
        repo.load_ready_build_facts.assert_awaited_once_with(42)

    @pytest.mark.asyncio
    async def test_raises_blocked_when_article_missing(self) -> None:
        repo = _repo_mock(missing=True)

        with pytest.raises(CurationReadyBuildBlockedError) as exc_info:
            await ReadyForCuration.try_advance_from(article_id=99, repo=repo)

        assert exc_info.value.blocked.target_article_id == 99
        assert (
            exc_info.value.blocked.code is CurationReadyBuildBlockedCode.ARTICLE_MISSING
        )
        repo.load_ready_build_facts.assert_awaited_once_with(99)

    @pytest.mark.asyncio
    async def test_raises_blocked_when_signal_exists(self) -> None:
        repo = _repo_mock(facts=_facts(has_signal_curation=True))

        with pytest.raises(CurationReadyBuildBlockedError) as exc_info:
            await ReadyForCuration.try_advance_from(article_id=42, repo=repo)

        assert (
            exc_info.value.blocked.code is CurationReadyBuildBlockedCode.ALREADY_CURATED
        )
        assert exc_info.value.blocked.source_name == "MIT News"
        repo.load_ready_build_facts.assert_awaited_once_with(42)

    @pytest.mark.asyncio
    async def test_raises_blocked_when_noise_exists(self) -> None:
        repo = _repo_mock(facts=_facts(has_noise_curation=True))

        with pytest.raises(CurationReadyBuildBlockedError) as exc_info:
            await ReadyForCuration.try_advance_from(article_id=42, repo=repo)

        assert (
            exc_info.value.blocked.code
            is CurationReadyBuildBlockedCode.ALREADY_REJECTED_AS_NOISE
        )
        assert exc_info.value.blocked.source_name == "MIT News"
        repo.load_ready_build_facts.assert_awaited_once_with(42)

    @pytest.mark.asyncio
    async def test_raises_blocked_when_content_too_large(self) -> None:
        oversized = "x" * (ReadyForCuration.MAX_CONTENT_LENGTH + 1)
        repo = _repo_mock(facts=_facts(content=oversized))

        with pytest.raises(CurationReadyBuildBlockedError) as exc_info:
            await ReadyForCuration.try_advance_from(article_id=42, repo=repo)

        blocked = exc_info.value.blocked
        assert blocked.code is CurationReadyBuildBlockedCode.CONTENT_TOO_LARGE
        assert blocked.content_length == len(oversized)
        assert blocked.max_content_length == ReadyForCuration.MAX_CONTENT_LENGTH
        assert blocked.source_name == "MIT News"


class TestReadyForCurationFieldConstraints:
    def test_rejects_empty_original_title(self) -> None:
        with pytest.raises(ValidationError):
            ReadyForCuration(article_id=1, original_title="", original_content="x")

    def test_rejects_empty_original_content(self) -> None:
        with pytest.raises(ValidationError):
            ReadyForCuration(article_id=1, original_title="t", original_content="")

    def test_rejects_oversized_original_content(self) -> None:
        oversized = "x" * (ReadyForCuration.MAX_CONTENT_LENGTH + 1)
        with pytest.raises(ValidationError):
            ReadyForCuration(
                article_id=1, original_title="t", original_content=oversized
            )

    def test_rejects_non_positive_article_id(self) -> None:
        with pytest.raises(ValidationError):
            ReadyForCuration(article_id=0, original_title="t", original_content="x")
        with pytest.raises(ValidationError):
            ReadyForCuration(article_id=-1, original_title="t", original_content="x")

    def test_is_frozen(self) -> None:
        ready = ReadyForCuration(article_id=1, original_title="t", original_content="x")
        with pytest.raises(ValidationError):
            ready.article_id = 999  # type: ignore[misc]


class TestCurationTrigger:
    def test_carries_article_id_only(self) -> None:
        trigger = CurationTrigger(article_id=42)
        assert trigger.article_id == 42

    def test_rejects_non_positive_article_id(self) -> None:
        with pytest.raises(ValidationError):
            CurationTrigger(article_id=0)
        with pytest.raises(ValidationError):
            CurationTrigger(article_id=-1)
