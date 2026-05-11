"""ReadyForAssessment (Stage 4 precondition 型) のドメインユニットテスト。

`try_advance_from` の precondition 充足 / 未充足 を Repository protocol mock で
検証する (DB 不要)。BaseModel(frozen=True) の不変性も確認。

注 (PR3.5-d.0): ファイル名 ``test_ready_for_classification.py`` は本 PR で
rename しない (別 cleanup PR で ``test_ready_for_assessment.py`` に rename
予定)。内容は assessment 命名に追従済。
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.analysis.assessment.domain.ready import ReadyForAssessment
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


def _make_repo_mock(
    *,
    in_scope_exists: bool = False,
    out_of_scope_exists: bool = False,
) -> AsyncMock:
    """``AssessmentExistenceProtocol`` を満たす 1 個の Repository mock。

    1 class に統合された ``AssessmentRepository`` に対応 (旧 2 個分割は廃止)。
    """
    repo = AsyncMock()
    repo.exists_in_scope = AsyncMock(return_value=in_scope_exists)
    repo.exists_out_of_scope = AsyncMock(return_value=out_of_scope_exists)
    return repo


# ---------------------------------------------------------------------------
# try_advance_from — precondition 充足 / 未充足
# ---------------------------------------------------------------------------


class TestTryAdvanceFromPreconditionMet:
    @pytest.mark.asyncio
    async def test_returns_ready_when_neither_in_scope_nor_out_of_scope_exists(
        self,
    ) -> None:
        """InScope / OutOfScope 評価ともに未生成なら Ready を返す。"""
        extraction = _make_extraction(id=42)
        repo = _make_repo_mock()

        ready = await ReadyForAssessment.try_advance_from(extraction, repo=repo)

        assert ready is not None
        assert ready.extraction_id == 42
        assert ready.translated_title == extraction.translated_title
        assert ready.summary == extraction.summary

    @pytest.mark.asyncio
    async def test_calls_exists_with_extraction_id(self) -> None:
        """exists 判定は extraction.id をキーに行う。"""
        extraction = _make_extraction(id=99)
        repo = _make_repo_mock()

        await ReadyForAssessment.try_advance_from(extraction, repo=repo)

        repo.exists_in_scope.assert_awaited_once_with(99)
        repo.exists_out_of_scope.assert_awaited_once_with(99)


class TestTryAdvanceFromPreconditionNotMet:
    @pytest.mark.asyncio
    async def test_returns_none_when_in_scope_already_exists(self) -> None:
        """同 extraction_id に InScopeAssessment が既存なら None を返す (業務正常)。"""
        extraction = _make_extraction(id=42)
        repo = _make_repo_mock(in_scope_exists=True)

        ready = await ReadyForAssessment.try_advance_from(extraction, repo=repo)

        assert ready is None
        # exists_out_of_scope は short-circuit で呼ばれない
        repo.exists_out_of_scope.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_none_when_out_of_scope_already_exists(self) -> None:
        """同 extraction_id に OutOfScopeAssessment 既存なら None を返す (業務正常)。"""
        extraction = _make_extraction(id=42)
        repo = _make_repo_mock(out_of_scope_exists=True)

        ready = await ReadyForAssessment.try_advance_from(extraction, repo=repo)

        assert ready is None
        repo.exists_in_scope.assert_awaited_once()
        repo.exists_out_of_scope.assert_awaited_once()


# ---------------------------------------------------------------------------
# Ready 型の不変条件
# ---------------------------------------------------------------------------


class TestReadyForAssessmentImmutability:
    def test_is_frozen(self) -> None:
        """frozen=True のため field 書き換えは ValidationError。"""
        ready = ReadyForAssessment(
            extraction_id=2,
            translated_title="title",
            summary="summary",
        )
        with pytest.raises(ValidationError):
            ready.extraction_id = 999  # type: ignore[misc]

    def test_validates_int_fields(self) -> None:
        """構築時に Pydantic が int を validate する。"""
        with pytest.raises(ValidationError):
            ReadyForAssessment(
                extraction_id="not-an-int",  # type: ignore[arg-type]
                translated_title="t",
                summary="s",
            )

    def test_field_shape_matches_legacy_classification(self) -> None:
        """taskiq in-flight 互換: ReadyForAssessment の field 構造は
        旧 ReadyForClassification と完全一致する。

        本 invariant が崩れると broker queue に残った旧 message の
        deserialize で field 不整合が発生する (本 PR の最重要 invariant)。
        """
        assert set(ReadyForAssessment.model_fields) == {
            "extraction_id",
            "translated_title",
            "summary",
        }
