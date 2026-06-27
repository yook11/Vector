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
    summary: str = "分析要約",
    key_points: object = None,
) -> EmbeddingReadyBuildFacts:
    return EmbeddingReadyBuildFacts(
        analyzable_article_id=analyzable_article_id,
        has_embedding=has_embedding,
        summary=summary,
        key_points=key_points,
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
        repo = _repo_mock(
            facts=_facts(
                key_points=[
                    {
                        "content": "OpenAIが新モデルを発表。",
                        "mentions": [
                            {"surface": "OpenAI", "type": "company"},
                            {"surface": "GPT-5", "type": "product"},
                        ],
                    },
                    {
                        "content": "NVIDIAがBlackwell出荷を拡大。",
                        "mentions": [
                            {"surface": "NVIDIA", "type": "company"},
                            {"surface": "Blackwell", "type": "product"},
                            {"surface": "nvidia", "type": "company"},
                        ],
                    },
                ],
            )
        )

        ready = await ReadyForEmbedding.try_advance_from(
            analyzed_article_id=100, embedding_repo=repo
        )

        assert ready == ReadyForEmbedding(
            analyzed_article_id=100,
            text_for_embedding=(
                "分析要約\n\n"
                "OpenAIが新モデルを発表。\n"
                "NVIDIAがBlackwell出荷を拡大。\n\n"
                "OpenAI, GPT-5, NVIDIA, Blackwell"
            ),
            analyzable_article_id=42,
        )
        assert "分析タイトル" not in ready.text_for_embedding
        assert "company" not in ready.text_for_embedding
        assert "product" not in ready.text_for_embedding
        assert "要約:" not in ready.text_for_embedding
        assert "重要ポイント:" not in ready.text_for_embedding
        assert "登場固有名:" not in ready.text_for_embedding
        repo.load_ready_build_facts.assert_awaited_once_with(100)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key_points", [None, [], {"content": "ignored"}])
    async def test_builds_summary_only_when_key_points_absent_or_malformed(
        self, key_points: object
    ) -> None:
        repo = _repo_mock(facts=_facts(key_points=key_points))

        ready = await ReadyForEmbedding.try_advance_from(
            analyzed_article_id=100, embedding_repo=repo
        )

        assert ready.text_for_embedding == "分析要約"

    @pytest.mark.asyncio
    async def test_ignores_malformed_key_point_items(self) -> None:
        repo = _repo_mock(
            facts=_facts(
                key_points=[
                    {"mentions": [{"surface": "IgnoredCo", "type": "company"}]},
                    {"content": 123, "mentions": []},
                    {"content": "", "mentions": []},
                    "not-a-dict",
                ],
            )
        )

        ready = await ReadyForEmbedding.try_advance_from(
            analyzed_article_id=100, embedding_repo=repo
        )

        assert ready.text_for_embedding == "分析要約"

    @pytest.mark.asyncio
    async def test_caps_mentions_at_thirty(self) -> None:
        mentions = [
            {"surface": f"Entity {index:02d}", "type": "company"} for index in range(31)
        ]
        repo = _repo_mock(
            facts=_facts(
                key_points=[
                    {
                        "content": "多数の固有名が登場した。",
                        "mentions": mentions,
                    }
                ]
            )
        )

        ready = await ReadyForEmbedding.try_advance_from(
            analyzed_article_id=100, embedding_repo=repo
        )

        mention_line = ready.text_for_embedding.split("\n\n")[-1]
        assert mention_line.split(", ") == [
            f"Entity {index:02d}" for index in range(30)
        ]

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
