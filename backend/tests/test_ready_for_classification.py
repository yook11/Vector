"""ReadyForAssessment (Stage 4 precondition 型) のドメインユニットテスト。

`try_advance_from` の precondition 充足 / 未充足 を Repository protocol mock で
検証する (DB 不要)。BaseModel(frozen=True) の不変性、新規 ``AssessmentTrigger``
の構造 + 旧 ``ReadyForAssessment`` message 受信互換も確認する。

注: ファイル名 ``test_ready_for_classification.py`` は別 cleanup PR で
``test_ready_for_assessment.py`` に rename 予定。内容は assessment 命名に
追従済。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.analysis.assessment.domain.ready import (
    AssessmentTrigger,
    ReadyForAssessment,
)


def _make_ready(**overrides: object) -> ReadyForAssessment:
    """Test fixtures 用の Ready 構築 helper (5 fields 既定値)。"""
    defaults: dict[str, object] = {
        "curation_id": 42,
        "translated_title": "量子コンピューティングの新たなブレイクスルー",
        "summary": "MIT が新手法を発表。量子エラー訂正の分野で大きな進展。",
        "article_id": 7,
        "source_name": "MIT News",
    }
    defaults.update(overrides)
    return ReadyForAssessment(**defaults)  # type: ignore[arg-type]


def _make_repo_mock(
    *,
    return_ready: ReadyForAssessment | None = None,
) -> AsyncMock:
    """``AssessmentPreconditionProtocol`` を満たす Repository mock。

    案 3: try_load_for_assessment が 1 query で Ready (または None) を返す。
    """
    repo = AsyncMock()
    repo.try_load_for_assessment = AsyncMock(return_value=return_ready)
    return repo


# ---------------------------------------------------------------------------
# try_advance_from — precondition 充足 / 未充足
# ---------------------------------------------------------------------------


class TestTryAdvanceFromPreconditionMet:
    @pytest.mark.asyncio
    async def test_returns_ready_from_repo(self) -> None:
        """Repository が Ready を返したら同 instance を返す (thin delegate)。"""
        expected = _make_ready(curation_id=42)
        repo = _make_repo_mock(return_ready=expected)

        ready = await ReadyForAssessment.try_advance_from(curation_id=42, repo=repo)

        assert ready is expected

    @pytest.mark.asyncio
    async def test_calls_repo_with_curation_id(self) -> None:
        """Repository には curation_id がそのまま渡される。"""
        expected = _make_ready(curation_id=99)
        repo = _make_repo_mock(return_ready=expected)

        await ReadyForAssessment.try_advance_from(curation_id=99, repo=repo)

        repo.try_load_for_assessment.assert_awaited_once_with(99)


class TestTryAdvanceFromPreconditionNotMet:
    @pytest.mark.asyncio
    async def test_returns_none_when_repo_returns_none(self) -> None:
        """Repository が None を返したら None を返す (業務正常状態)。"""
        repo = _make_repo_mock(return_ready=None)

        ready = await ReadyForAssessment.try_advance_from(curation_id=42, repo=repo)

        assert ready is None
        repo.try_load_for_assessment.assert_awaited_once_with(42)


# ---------------------------------------------------------------------------
# Ready 型の不変条件
# ---------------------------------------------------------------------------


class TestReadyForAssessmentImmutability:
    def test_is_frozen(self) -> None:
        """frozen=True のため field 書き換えは ValidationError。"""
        ready = _make_ready()
        with pytest.raises(ValidationError):
            ready.curation_id = 999  # type: ignore[misc]

    def test_validates_int_fields(self) -> None:
        """構築時に Pydantic が int を validate する。"""
        with pytest.raises(ValidationError):
            ReadyForAssessment(
                curation_id="not-an-int",  # type: ignore[arg-type]
                translated_title="t",
                summary="s",
                article_id=1,
                source_name=None,
            )

    def test_rejects_non_positive_curation_id(self) -> None:
        """curation_id は gt=0 (Field constraint)。"""
        with pytest.raises(ValidationError):
            ReadyForAssessment(
                curation_id=0,
                translated_title="t",
                summary="s",
                article_id=1,
                source_name=None,
            )

    def test_rejects_non_positive_article_id(self) -> None:
        """article_id は gt=0 (Field constraint)。"""
        with pytest.raises(ValidationError):
            ReadyForAssessment(
                curation_id=1,
                translated_title="t",
                summary="s",
                article_id=0,
                source_name=None,
            )

    def test_accepts_none_source_name(self) -> None:
        """source_name は NewsSource 不在 / FK 切断時に None を許容する。"""
        ready = ReadyForAssessment(
            curation_id=1,
            translated_title="t",
            summary="s",
            article_id=1,
            source_name=None,
        )
        assert ready.source_name is None


# ---------------------------------------------------------------------------
# AssessmentTrigger — kiq message 用 ID キャリア
# ---------------------------------------------------------------------------


class TestAssessmentTrigger:
    def test_is_frozen(self) -> None:
        trigger = AssessmentTrigger(curation_id=42)
        with pytest.raises(ValidationError):
            trigger.curation_id = 999  # type: ignore[misc]

    def test_rejects_non_positive_curation_id(self) -> None:
        with pytest.raises(ValidationError):
            AssessmentTrigger(curation_id=0)

    def test_accepts_legacy_extraction_id_alias(self) -> None:
        """旧 in-flight message (`extraction_id` field) を新 schema が
        ``validation_alias=AliasChoices("curation_id", "extraction_id")`` で
        受け入れる (rolling deploy 互換、PR-E.3 で alias 削除予定)。
        """
        trigger = AssessmentTrigger.model_validate(
            {
                "extraction_id": 1,
                "translated_title": "t",
                "summary": "s",
            }
        )
        assert trigger.curation_id == 1

    def test_serializes_with_curation_id_alias(self) -> None:
        """``model_dump(by_alias=True)`` は新 field 名 ``curation_id`` で出力。"""
        trigger = AssessmentTrigger(curation_id=42)
        assert trigger.model_dump(by_alias=True) == {"curation_id": 42}
