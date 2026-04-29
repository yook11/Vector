"""ReadyForClassification (Stage D precondition 型) のドメインユニットテスト。

`try_advance_from` の precondition 充足 / 未充足 を Repository protocol mock で
検証する (DB 不要)。BaseModel(frozen=True) の不変性も確認。
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.analysis.classification.domain.ready import ReadyForClassification
from app.analysis.extraction.domain.extraction import Extraction


def _make_extraction(**overrides: object) -> Extraction:
    defaults: dict[str, object] = {
        "id": 42,
        "translated_title": "量子コンピューティングの新たなブレイクスルー",
        "summary": "MIT が新手法を発表。量子エラー訂正の分野で大きな進展。",
        "entities": (),
        "ai_model": "gemini-2.5-flash-lite",
        "extracted_at": datetime(2026, 4, 28, tzinfo=UTC),
    }
    defaults.update(overrides)
    return Extraction(**defaults)  # type: ignore[arg-type]


def _make_repo_mocks(
    *,
    analysis_exists: bool = False,
    rejection_exists: bool = False,
) -> tuple[AsyncMock, AsyncMock]:
    analysis_repo = AsyncMock()
    analysis_repo.exists_for_extraction = AsyncMock(return_value=analysis_exists)
    rejection_repo = AsyncMock()
    rejection_repo.exists_for_extraction = AsyncMock(return_value=rejection_exists)
    return analysis_repo, rejection_repo


# ---------------------------------------------------------------------------
# try_advance_from — precondition 充足 / 未充足
# ---------------------------------------------------------------------------


class TestTryAdvanceFromPreconditionMet:
    @pytest.mark.asyncio
    async def test_returns_ready_when_neither_analysis_nor_rejection_exists(
        self,
    ) -> None:
        """Analysis / Rejection 共に未生成なら Ready を返す。"""
        extraction = _make_extraction(id=42)
        analysis_repo, rejection_repo = _make_repo_mocks()

        ready = await ReadyForClassification.try_advance_from(
            extraction,
            analysis_repo=analysis_repo,
            rejection_repo=rejection_repo,
        )

        assert ready is not None
        assert ready.extraction_id == 42
        assert ready.translated_title == extraction.translated_title
        assert ready.summary == extraction.summary

    @pytest.mark.asyncio
    async def test_calls_exists_for_extraction_with_extraction_id(self) -> None:
        """exists 判定は extraction.id をキーに行う。"""
        extraction = _make_extraction(id=99)
        analysis_repo, rejection_repo = _make_repo_mocks()

        await ReadyForClassification.try_advance_from(
            extraction,
            analysis_repo=analysis_repo,
            rejection_repo=rejection_repo,
        )

        analysis_repo.exists_for_extraction.assert_awaited_once_with(99)
        rejection_repo.exists_for_extraction.assert_awaited_once_with(99)


class TestTryAdvanceFromPreconditionNotMet:
    @pytest.mark.asyncio
    async def test_returns_none_when_analysis_already_exists(self) -> None:
        """同 extraction_id に Analysis が既存なら None を返す (業務正常)。"""
        extraction = _make_extraction(id=42)
        analysis_repo, rejection_repo = _make_repo_mocks(analysis_exists=True)

        ready = await ReadyForClassification.try_advance_from(
            extraction,
            analysis_repo=analysis_repo,
            rejection_repo=rejection_repo,
        )

        assert ready is None
        # rejection_repo は short-circuit で呼ばれない
        rejection_repo.exists_for_extraction.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_none_when_rejection_already_exists(self) -> None:
        """同 extraction_id に Rejection が既存なら None を返す (業務正常)。"""
        extraction = _make_extraction(id=42)
        analysis_repo, rejection_repo = _make_repo_mocks(rejection_exists=True)

        ready = await ReadyForClassification.try_advance_from(
            extraction,
            analysis_repo=analysis_repo,
            rejection_repo=rejection_repo,
        )

        assert ready is None
        analysis_repo.exists_for_extraction.assert_awaited_once()
        rejection_repo.exists_for_extraction.assert_awaited_once()


# ---------------------------------------------------------------------------
# Ready 型の不変条件
# ---------------------------------------------------------------------------


class TestReadyForClassificationImmutability:
    def test_is_frozen(self) -> None:
        """frozen=True のため field 書き換えは ValidationError。"""
        ready = ReadyForClassification(
            extraction_id=2,
            translated_title="title",
            summary="summary",
        )
        with pytest.raises(ValidationError):
            ready.extraction_id = 999  # type: ignore[misc]

    def test_validates_int_fields(self) -> None:
        """構築時に Pydantic が int を validate する。"""
        with pytest.raises(ValidationError):
            ReadyForClassification(
                extraction_id="not-an-int",  # type: ignore[arg-type]
                translated_title="t",
                summary="s",
            )
