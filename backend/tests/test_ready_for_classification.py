"""ReadyForAssessment (Stage 4 precondition 型) のドメインユニットテスト。"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.analysis.assessment.domain.ready import (
    AssessmentReadyBuildBlockedCode,
    AssessmentReadyBuildBlockedError,
    AssessmentReadyBuildFacts,
    ReadyForAssessment,
)
from app.queue.messages.assessment import AssessmentTrigger


def _facts(
    *,
    curation_id: int = 42,
    article_id: int = 7,
    title: str = "量子コンピューティングの新たなブレイクスルー",
    summary: str = "MIT が新手法を発表。量子エラー訂正の分野で大きな進展。",
    source_name: str | None = "MIT News",
    has_in_scope_assessment: bool = False,
    has_out_of_scope_assessment: bool = False,
) -> AssessmentReadyBuildFacts:
    return AssessmentReadyBuildFacts(
        curation_id=curation_id,
        article_id=article_id,
        translated_title=title,
        summary=summary,
        source_name=source_name,
        has_in_scope_assessment=has_in_scope_assessment,
        has_out_of_scope_assessment=has_out_of_scope_assessment,
    )


def _make_ready(**overrides: object) -> ReadyForAssessment:
    defaults: dict[str, object] = {
        "curation_id": 42,
        "translated_title": "量子コンピューティングの新たなブレイクスルー",
        "summary": "MIT が新手法を発表。量子エラー訂正の分野で大きな進展。",
        "article_id": 7,
        "source_name": "MIT News",
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
    async def test_builds_ready_from_repository_facts(self) -> None:
        repo = _repo_mock(facts=_facts(curation_id=42))

        ready = await ReadyForAssessment.try_advance_from(curation_id=42, repo=repo)

        assert ready == _make_ready(curation_id=42)
        repo.load_ready_build_facts.assert_awaited_once_with(42)

    @pytest.mark.asyncio
    async def test_raises_blocked_when_curation_missing(self) -> None:
        repo = _repo_mock(missing=True)

        with pytest.raises(AssessmentReadyBuildBlockedError) as exc_info:
            await ReadyForAssessment.try_advance_from(curation_id=42, repo=repo)

        assert exc_info.value.code is AssessmentReadyBuildBlockedCode.CURATION_MISSING
        repo.load_ready_build_facts.assert_awaited_once_with(42)

    @pytest.mark.asyncio
    async def test_raises_blocked_when_in_scope_exists(self) -> None:
        repo = _repo_mock(facts=_facts(has_in_scope_assessment=True))

        with pytest.raises(AssessmentReadyBuildBlockedError) as exc_info:
            await ReadyForAssessment.try_advance_from(curation_id=42, repo=repo)

        assert exc_info.value.code is AssessmentReadyBuildBlockedCode.ALREADY_IN_SCOPE
        repo.load_ready_build_facts.assert_awaited_once_with(42)

    @pytest.mark.asyncio
    async def test_raises_blocked_when_out_of_scope_exists(self) -> None:
        repo = _repo_mock(facts=_facts(has_out_of_scope_assessment=True))

        with pytest.raises(AssessmentReadyBuildBlockedError) as exc_info:
            await ReadyForAssessment.try_advance_from(curation_id=42, repo=repo)

        assert (
            exc_info.value.code is AssessmentReadyBuildBlockedCode.ALREADY_OUT_OF_SCOPE
        )
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
                article_id=1,
                source_name=None,
            )

    def test_rejects_non_positive_curation_id(self) -> None:
        with pytest.raises(ValidationError):
            ReadyForAssessment(
                curation_id=0,
                translated_title="t",
                summary="s",
                article_id=1,
                source_name=None,
            )

    def test_rejects_non_positive_article_id(self) -> None:
        with pytest.raises(ValidationError):
            ReadyForAssessment(
                curation_id=1,
                translated_title="t",
                summary="s",
                article_id=0,
                source_name=None,
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
