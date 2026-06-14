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
    analyzable_article_id: int = 42,
) -> EmbeddingReadyBuildFacts:
    return EmbeddingReadyBuildFacts(
        analyzable_article_id=analyzable_article_id,
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
            analyzed_article_id=100, embedding_repo=repo
        )

        assert ready == ReadyForEmbedding(
            analyzed_article_id=100,
            text_for_embedding="分析タイトル\n分析要約",
            analyzable_article_id=42,
        )
        repo.load_ready_build_facts.assert_awaited_once_with(100)

    @pytest.mark.asyncio
    async def test_raises_blocked_when_analyzed_article_missing(self) -> None:
        repo = _repo_mock(missing=True)

        with pytest.raises(EmbeddingReadyBuildBlockedError) as exc_info:
            await ReadyForEmbedding.try_advance_from(
                analyzed_article_id=100, embedding_repo=repo
            )

        assert (
            exc_info.value.code
            is EmbeddingReadyBuildBlockedCode.ANALYZED_ARTICLE_MISSING
        )
        repo.load_ready_build_facts.assert_awaited_once_with(100)

    @pytest.mark.asyncio
    async def test_raises_blocked_when_already_embedded(self) -> None:
        repo = _repo_mock(facts=_facts(has_embedding=True))

        with pytest.raises(EmbeddingReadyBuildBlockedError) as exc_info:
            await ReadyForEmbedding.try_advance_from(
                analyzed_article_id=100, embedding_repo=repo
            )

        assert exc_info.value.code is EmbeddingReadyBuildBlockedCode.ALREADY_EMBEDDED
        repo.load_ready_build_facts.assert_awaited_once_with(100)

    @pytest.mark.asyncio
    async def test_rejects_legacy_analysis_id_keyword(self) -> None:
        repo = _repo_mock()

        with pytest.raises(TypeError):
            await ReadyForEmbedding.try_advance_from(
                analysis_id=100, embedding_repo=repo
            )


class TestReadyForEmbeddingFieldConstraints:
    def test_rejects_non_positive_analyzed_article_id(self) -> None:
        with pytest.raises(ValidationError):
            ReadyForEmbedding(
                analyzed_article_id=0, text_for_embedding="t", analyzable_article_id=1
            )
        with pytest.raises(ValidationError):
            ReadyForEmbedding(
                analyzed_article_id=-1, text_for_embedding="t", analyzable_article_id=1
            )

    def test_rejects_empty_text(self) -> None:
        with pytest.raises(ValidationError):
            ReadyForEmbedding(
                analyzed_article_id=1, text_for_embedding="", analyzable_article_id=1
            )

    def test_rejects_non_positive_analyzable_article_id(self) -> None:
        with pytest.raises(ValidationError):
            ReadyForEmbedding(
                analyzed_article_id=1, text_for_embedding="t", analyzable_article_id=0
            )
        with pytest.raises(ValidationError):
            ReadyForEmbedding(
                analyzed_article_id=1, text_for_embedding="t", analyzable_article_id=-1
            )

    def test_rejects_legacy_analysis_id_alias(self) -> None:
        with pytest.raises(ValidationError):
            ReadyForEmbedding(
                analysis_id=1, text_for_embedding="t", analyzable_article_id=1
            )

    def test_is_frozen(self) -> None:
        ready = ReadyForEmbedding(
            analyzed_article_id=1, text_for_embedding="t\ns", analyzable_article_id=1
        )
        with pytest.raises(ValidationError):
            ready.analyzed_article_id = 999  # type: ignore[misc]


class TestEmbeddingTrigger:
    def test_carries_analyzed_article_id_only(self) -> None:
        trigger = EmbeddingTrigger(analyzed_article_id=42)
        assert trigger.model_dump() == {"analyzed_article_id": 42}

    def test_rejects_legacy_analysis_id_alias(self) -> None:
        with pytest.raises(ValidationError):
            EmbeddingTrigger(analysis_id=42)

    def test_rejects_non_positive_analyzed_article_id(self) -> None:
        with pytest.raises(ValidationError):
            EmbeddingTrigger(analyzed_article_id=0)
        with pytest.raises(ValidationError):
            EmbeddingTrigger(analyzed_article_id=-1)
