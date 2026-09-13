"""ReadyForAssessment (Stage 4 precondition 型) のドメインユニットテスト。"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.analysis.assessment.domain.ready import (
    AssessmentReadyBuildFacts,
    AssessmentReadyBuildRejectionReason,
    ReadyForAssessment,
)
from app.queue.messages.assessment import AssessmentTrigger


def _facts(
    *,
    curation_id: int = 42,
    analyzable_article_id: int = 7,
    title: str = "量子コンピューティングの新たなブレイクスルー",
    summary: str = "MIT が新手法を発表。量子エラー訂正の分野で大きな進展。",
    has_analyzed_article: bool = False,
    has_out_of_scope_article: bool = False,
) -> AssessmentReadyBuildFacts:
    return AssessmentReadyBuildFacts(
        curation_id=curation_id,
        analyzable_article_id=analyzable_article_id,
        translated_title=title,
        summary=summary,
        has_analyzed_article=has_analyzed_article,
        has_out_of_scope_article=has_out_of_scope_article,
    )


def _make_ready(**overrides: object) -> ReadyForAssessment:
    defaults: dict[str, object] = {
        "curation_id": 42,
        "translated_title": "量子コンピューティングの新たなブレイクスルー",
        "summary": "MIT が新手法を発表。量子エラー訂正の分野で大きな進展。",
    }
    defaults.update(overrides)
    return ReadyForAssessment(**defaults)  # type: ignore[arg-type]


def _repo_mock(
    *,
    facts: AssessmentReadyBuildFacts | None = None,
    missing: bool = False,
) -> AsyncMock:
    repo = AsyncMock()
    repo.load_ready_build_facts = AsyncMock(
        return_value=None if missing else facts or _facts()
    )
    return repo


class TestTryAdvanceFrom:
    @pytest.mark.asyncio
    async def test_builds_ready_and_returns_facts_derived_subject(self) -> None:
        repo = _repo_mock(facts=_facts(curation_id=42, analyzable_article_id=7))

        ready, analyzable_article_id = await ReadyForAssessment.try_advance_from(
            curation_id=42, repo=repo
        )

        assert ready == _make_ready(curation_id=42)
        # 監査主語は Ready に載せず facts 由来の値を外で返す (_facts の default = 7)
        assert analyzable_article_id == 7
        repo.load_ready_build_facts.assert_awaited_once_with(42)

    @pytest.mark.asyncio
    async def test_returns_rejection_when_curation_missing(self) -> None:
        repo = _repo_mock(missing=True)

        rejected = await ReadyForAssessment.try_advance_from(curation_id=42, repo=repo)

        assert rejected.reason is AssessmentReadyBuildRejectionReason.CURATION_MISSING
        # facts 無く analyzable_article_id は運べない (audit の source_id も空)
        assert rejected.analyzable_article_id is None
        repo.load_ready_build_facts.assert_awaited_once_with(42)

    @pytest.mark.asyncio
    async def test_raises_blocked_when_in_scope_exists(self) -> None:
        repo = _repo_mock(
            facts=_facts(has_analyzed_article=True, analyzable_article_id=7)
        )

        rejected = await ReadyForAssessment.try_advance_from(curation_id=42, repo=repo)

        assert rejected.reason is AssessmentReadyBuildRejectionReason.ALREADY_IN_SCOPE
        # 拒否値の記事IDはDB由来の事実を保持する。
        assert rejected.analyzable_article_id == 7
        repo.load_ready_build_facts.assert_awaited_once_with(42)

    @pytest.mark.asyncio
    async def test_raises_blocked_when_out_of_scope_exists(self) -> None:
        repo = _repo_mock(
            facts=_facts(has_out_of_scope_article=True, analyzable_article_id=7)
        )

        rejected = await ReadyForAssessment.try_advance_from(curation_id=42, repo=repo)

        assert (
            rejected.reason is AssessmentReadyBuildRejectionReason.ALREADY_OUT_OF_SCOPE
        )
        # 拒否値の記事IDはDB由来の事実を保持する。
        assert rejected.analyzable_article_id == 7
        repo.load_ready_build_facts.assert_awaited_once_with(42)


class TestReadyForAssessmentImmutability:
    def test_is_frozen(self) -> None:
        ready = _make_ready()
        with pytest.raises(ValidationError):
            ready.curation_id = 999  # type: ignore[misc]

    def test_validates_int_fields(self) -> None:
        with pytest.raises(ValidationError):
            ReadyForAssessment(
                curation_id="not-an-int",  # type: ignore[arg-type]
                translated_title="t",
                summary="s",
            )

    def test_rejects_non_positive_curation_id(self) -> None:
        with pytest.raises(ValidationError):
            ReadyForAssessment(
                curation_id=0,
                translated_title="t",
                summary="s",
            )


class TestAssessmentTrigger:
    def test_carries_curation_id_only(self) -> None:
        trigger = AssessmentTrigger(curation_id=42)
        assert trigger.curation_id == 42

    def test_rejects_non_positive_curation_id(self) -> None:
        with pytest.raises(ValidationError):
            AssessmentTrigger(curation_id=0)
        with pytest.raises(ValidationError):
            AssessmentTrigger(curation_id=-1)


def test_rejection_reasons_partition_idempotent_skip_from_durable() -> None:
    """処理済みだけを監査対象から除き、欠損と入力不正の理由を記録する。"""
    idempotent = {
        c for c in AssessmentReadyBuildRejectionReason if c.is_idempotent_skip
    }
    durable = {
        c for c in AssessmentReadyBuildRejectionReason if not c.is_idempotent_skip
    }
    assert idempotent == {
        AssessmentReadyBuildRejectionReason.ALREADY_IN_SCOPE,
        AssessmentReadyBuildRejectionReason.ALREADY_OUT_OF_SCOPE,
    }
    assert durable == {
        AssessmentReadyBuildRejectionReason.CURATION_MISSING,
        AssessmentReadyBuildRejectionReason.INPUT_INVALID,
    }
