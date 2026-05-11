"""ReadyForEmbedding (Stage 5 precondition 型) のドメインユニットテスト。

`try_advance_from` の precondition 充足 / 未充足 を Repository protocol mock で
検証する (DB 不要)。BaseModel(frozen=True) の不変性 + Field 制約も確認。

2026-05-12 改修 (案 3): Ready は厚い型 (analysis_id + text_for_embedding の全揃え)
として運ばれ、Repository が atomic な 1 query で precondition 判定 + Ready 構築を
完結させる。`try_advance_from` は Repository の `try_load_for_embedding` への
thin delegate。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.analysis.embedding.domain.ready import (
    EmbeddingTrigger,
    ReadyForEmbedding,
)


def _make_repo_mock(*, ready: ReadyForEmbedding | None = None) -> AsyncMock:
    repo = AsyncMock()
    repo.try_load_for_embedding = AsyncMock(return_value=ready)
    return repo


# ---------------------------------------------------------------------------
# try_advance_from — precondition 充足 / 未充足
# ---------------------------------------------------------------------------


class TestTryAdvanceFromPreconditionMet:
    @pytest.mark.asyncio
    async def test_returns_ready_when_repo_loads_one(self) -> None:
        """Repository が Ready を返したらそのまま delegate して返す。"""
        loaded = ReadyForEmbedding(
            analysis_id=100,
            text_for_embedding="分析タイトル\n分析要約",
        )
        repo = _make_repo_mock(ready=loaded)

        ready = await ReadyForEmbedding.try_advance_from(
            analysis_id=100, embedding_repo=repo
        )

        assert ready is loaded
        assert ready.text_for_embedding == "分析タイトル\n分析要約"

    @pytest.mark.asyncio
    async def test_calls_try_load_for_embedding_with_analysis_id(self) -> None:
        """gateway は analysis_id をそのまま Repository に渡す。"""
        repo = _make_repo_mock(
            ready=ReadyForEmbedding(
                analysis_id=777,
                text_for_embedding="t\ns",
            )
        )

        await ReadyForEmbedding.try_advance_from(analysis_id=777, embedding_repo=repo)

        repo.try_load_for_embedding.assert_awaited_once_with(777)


class TestTryAdvanceFromPreconditionNotMet:
    @pytest.mark.asyncio
    async def test_returns_none_when_repo_returns_none(self) -> None:
        """Repository が None (行不在 or 既 embedded) を返したら None。"""
        repo = _make_repo_mock(ready=None)

        ready = await ReadyForEmbedding.try_advance_from(
            analysis_id=100, embedding_repo=repo
        )

        assert ready is None


# ---------------------------------------------------------------------------
# Ready 型の Field 制約 (Pydantic 構造保証)
# ---------------------------------------------------------------------------


class TestReadyForEmbeddingFieldConstraints:
    def test_rejects_non_positive_analysis_id(self) -> None:
        """analysis_id <= 0 は Field(gt=0) が拒否する。"""
        with pytest.raises(ValidationError):
            ReadyForEmbedding(analysis_id=0, text_for_embedding="t")
        with pytest.raises(ValidationError):
            ReadyForEmbedding(analysis_id=-1, text_for_embedding="t")

    def test_rejects_empty_text(self) -> None:
        """text_for_embedding 空文字は Field(min_length=1) が拒否する。"""
        with pytest.raises(ValidationError):
            ReadyForEmbedding(analysis_id=1, text_for_embedding="")

    def test_is_frozen(self) -> None:
        """frozen=True のため field 書き換えは ValidationError。"""
        ready = ReadyForEmbedding(analysis_id=1, text_for_embedding="t\ns")
        with pytest.raises(ValidationError):
            ready.analysis_id = 999  # type: ignore[misc]
        with pytest.raises(ValidationError):
            ready.text_for_embedding = "x"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# EmbeddingTrigger — kiq message 用軽量 ID キャリア
# ---------------------------------------------------------------------------


class TestEmbeddingTrigger:
    def test_holds_analysis_id(self) -> None:
        trigger = EmbeddingTrigger(analysis_id=42)
        assert trigger.analysis_id == 42

    def test_rejects_non_positive_analysis_id(self) -> None:
        with pytest.raises(ValidationError):
            EmbeddingTrigger(analysis_id=0)
        with pytest.raises(ValidationError):
            EmbeddingTrigger(analysis_id=-5)

    def test_is_frozen(self) -> None:
        trigger = EmbeddingTrigger(analysis_id=10)
        with pytest.raises(ValidationError):
            trigger.analysis_id = 999  # type: ignore[misc]
