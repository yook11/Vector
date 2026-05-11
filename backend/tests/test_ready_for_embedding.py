"""ReadyForEmbedding (Stage E precondition 型) のドメインユニットテスト。

`try_advance_from` の precondition 充足 / 未充足 を Repository protocol mock で
検証する (DB 不要)。BaseModel(frozen=True) の不変性 + Field 制約も確認。

2026-05-11 改修: Ready は ID + 構造 precondition のみを passport として運ぶ
(値は DB SSoT、`feedback_bc_boundary_guarantees_downstream`)。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.analysis.embedding.domain.ready import ReadyForEmbedding


def _make_repo_mock(*, is_embedded: bool = False) -> AsyncMock:
    repo = AsyncMock()
    repo.is_embedded_for = AsyncMock(return_value=is_embedded)
    return repo


# ---------------------------------------------------------------------------
# try_advance_from — precondition 充足 / 未充足
# ---------------------------------------------------------------------------


class TestTryAdvanceFromPreconditionMet:
    @pytest.mark.asyncio
    async def test_returns_ready_when_not_yet_embedded(self) -> None:
        """同 analysis_id に embedding 未生成なら Ready を返す。"""
        repo = _make_repo_mock(is_embedded=False)

        ready = await ReadyForEmbedding.try_advance_from(
            analysis_id=100, embedding_repo=repo
        )

        assert ready is not None
        assert ready.analysis_id == 100

    @pytest.mark.asyncio
    async def test_calls_is_embedded_for_with_analysis_id(self) -> None:
        """exists 判定は analysis_id をキーに行う。"""
        repo = _make_repo_mock(is_embedded=False)

        await ReadyForEmbedding.try_advance_from(analysis_id=777, embedding_repo=repo)

        repo.is_embedded_for.assert_awaited_once_with(777)


class TestTryAdvanceFromPreconditionNotMet:
    @pytest.mark.asyncio
    async def test_returns_none_when_already_embedded(self) -> None:
        """同 analysis_id に embedding 既存なら None を返す (業務正常)。"""
        repo = _make_repo_mock(is_embedded=True)

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
            ReadyForEmbedding(analysis_id=0)
        with pytest.raises(ValidationError):
            ReadyForEmbedding(analysis_id=-1)

    def test_is_frozen(self) -> None:
        """frozen=True のため field 書き換えは ValidationError。"""
        ready = ReadyForEmbedding(analysis_id=1)
        with pytest.raises(ValidationError):
            ready.analysis_id = 999  # type: ignore[misc]
