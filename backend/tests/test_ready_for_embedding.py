"""ReadyForEmbedding (Stage 5 precondition 型) のドメインユニットテスト。"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.analysis.embedding.domain.ready import (
    EmbeddingReadyBuildBlockedCode,
    EmbeddingReadyBuildBlockedError,
    EmbeddingReadyBuildFacts,
    ReadyForEmbedding,
)
from app.queue.messages.embedding import EmbeddingTrigger


def _facts(
    *,
    has_embedding: bool = False,
    article_id: int = 42,
) -> EmbeddingReadyBuildFacts:
    return EmbeddingReadyBuildFacts(
        article_id=article_id,
        has_embedding=has_embedding,
        translated_title="分析タイトル",
        summary="分析要約",
    )


def _repo_mock(
    *,
    facts: EmbeddingReadyBuildFacts | None = None,
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
        repo = _repo_mock()

        ready = await ReadyForEmbedding.try_advance_from(
            analysis_id=100, embedding_repo=repo
        )

        assert ready == ReadyForEmbedding(
            analysis_id=100,
            text_for_embedding="分析タイトル\n分析要約",
            article_id=42,
        )
        repo.load_ready_build_facts.assert_awaited_once_with(100)

    @pytest.mark.asyncio
    async def test_raises_blocked_when_analysis_missing(self) -> None:
        repo = _repo_mock(missing=True)

        with pytest.raises(EmbeddingReadyBuildBlockedError) as exc_info:
            await ReadyForEmbedding.try_advance_from(
                analysis_id=100, embedding_repo=repo
            )

        assert (
            exc_info.value.blocked.code
            is EmbeddingReadyBuildBlockedCode.ANALYSIS_MISSING
        )
        assert exc_info.value.blocked.analysis_id == 100
        repo.load_ready_build_facts.assert_awaited_once_with(100)

    @pytest.mark.asyncio
    async def test_raises_blocked_when_already_embedded(self) -> None:
        repo = _repo_mock(facts=_facts(has_embedding=True))

        with pytest.raises(EmbeddingReadyBuildBlockedError) as exc_info:
            await ReadyForEmbedding.try_advance_from(
                analysis_id=100, embedding_repo=repo
            )

        blocked = exc_info.value.blocked
        assert blocked.code is EmbeddingReadyBuildBlockedCode.ALREADY_EMBEDDED
        assert blocked.article_id == 42
        repo.load_ready_build_facts.assert_awaited_once_with(100)


class TestReadyForEmbeddingFieldConstraints:
    def test_rejects_non_positive_analysis_id(self) -> None:
        with pytest.raises(ValidationError):
            ReadyForEmbedding(analysis_id=0, text_for_embedding="t", article_id=1)
        with pytest.raises(ValidationError):
            ReadyForEmbedding(analysis_id=-1, text_for_embedding="t", article_id=1)

    def test_rejects_empty_text(self) -> None:
        with pytest.raises(ValidationError):
            ReadyForEmbedding(analysis_id=1, text_for_embedding="", article_id=1)

    def test_rejects_non_positive_article_id(self) -> None:
        with pytest.raises(ValidationError):
            ReadyForEmbedding(analysis_id=1, text_for_embedding="t", article_id=0)
        with pytest.raises(ValidationError):
            ReadyForEmbedding(analysis_id=1, text_for_embedding="t", article_id=-1)

    def test_is_frozen(self) -> None:
        ready = ReadyForEmbedding(
            analysis_id=1, text_for_embedding="t\ns", article_id=1
        )
        with pytest.raises(ValidationError):
            ready.analysis_id = 999  # type: ignore[misc]


class TestEmbeddingTrigger:
    def test_carries_analysis_id_only(self) -> None:
        trigger = EmbeddingTrigger(analysis_id=42)
        assert trigger.analysis_id == 42

    def test_rejects_non_positive_analysis_id(self) -> None:
        with pytest.raises(ValidationError):
            EmbeddingTrigger(analysis_id=0)
        with pytest.raises(ValidationError):
            EmbeddingTrigger(analysis_id=-1)
