"""ReadyForEmbedding (Stage E precondition 型) のドメインユニットテスト。

`try_advance_from` の precondition 充足 / 未充足 を Repository protocol mock で
検証する (DB 不要)。BaseModel(frozen=True) の不変性 + Field 制約も確認。
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.analysis.classification.domain.analysis import Analysis
from app.analysis.domain.value_objects.topic import TopicName
from app.analysis.embedding.domain.ready import ReadyForEmbedding


def _make_analysis(**overrides: object) -> Analysis:
    defaults: dict[str, object] = {
        "id": 100,
        "extraction_id": 42,
        "translated_title": "量子コンピューティングの新たなブレイクスルー",
        "summary": "MIT が新手法を発表。量子エラー訂正の分野で大きな進展。",
        "topic": TopicName("Quantum"),
        "category_id": 3,
        "investor_take": "量子ハードウェア関連株を注視",
        "ai_model": "deepseek-v4-flash",
        "analyzed_at": datetime(2026, 4, 28, tzinfo=UTC),
    }
    defaults.update(overrides)
    return Analysis(**defaults)  # type: ignore[arg-type]


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
        analysis = _make_analysis(id=100)
        repo = _make_repo_mock(is_embedded=False)

        ready = await ReadyForEmbedding.try_advance_from(analysis, repo)

        assert ready is not None
        assert ready.analysis_id == 100
        assert ready.text_for_embedding == (
            f"{analysis.translated_title}\n{analysis.summary}"
        )

    @pytest.mark.asyncio
    async def test_calls_is_embedded_for_with_analysis_id(self) -> None:
        """exists 判定は analysis.id をキーに行う。"""
        analysis = _make_analysis(id=777)
        repo = _make_repo_mock(is_embedded=False)

        await ReadyForEmbedding.try_advance_from(analysis, repo)

        repo.is_embedded_for.assert_awaited_once_with(777)


class TestTryAdvanceFromPreconditionNotMet:
    @pytest.mark.asyncio
    async def test_returns_none_when_already_embedded(self) -> None:
        """同 analysis_id に embedding 既存なら None を返す (業務正常)。"""
        analysis = _make_analysis(id=100)
        repo = _make_repo_mock(is_embedded=True)

        ready = await ReadyForEmbedding.try_advance_from(analysis, repo)

        assert ready is None


# ---------------------------------------------------------------------------
# Ready 型の Field 制約 (Pydantic 構造保証)
# ---------------------------------------------------------------------------


class TestReadyForEmbeddingFieldConstraints:
    def test_rejects_empty_text_for_embedding(self) -> None:
        """空文字 text_for_embedding は Field(min_length=1) が拒否する。"""
        with pytest.raises(ValidationError):
            ReadyForEmbedding(analysis_id=1, text_for_embedding="")

    def test_rejects_non_positive_analysis_id(self) -> None:
        """analysis_id <= 0 は Field(gt=0) が拒否する。"""
        with pytest.raises(ValidationError):
            ReadyForEmbedding(analysis_id=0, text_for_embedding="x")
        with pytest.raises(ValidationError):
            ReadyForEmbedding(analysis_id=-1, text_for_embedding="x")

    def test_is_frozen(self) -> None:
        """frozen=True のため field 書き換えは ValidationError。"""
        ready = ReadyForEmbedding(analysis_id=1, text_for_embedding="x")
        with pytest.raises(ValidationError):
            ready.analysis_id = 999  # type: ignore[misc]
