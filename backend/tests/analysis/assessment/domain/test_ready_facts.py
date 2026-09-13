"""取得済みの開始条件と既存の非同期入口の契約を検証する。"""

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from app.analysis.assessment.domain.ready import (
    AssessmentReadyBuildFacts,
    AssessmentReadyBuildRejectionReason,
    ReadyForAssessment,
)

_FACTS = AssessmentReadyBuildFacts(
    curation_id=5,
    analyzable_article_id=7,
    translated_title="title",
    summary="summary",
    has_analyzed_article=False,
    has_out_of_scope_article=False,
)


def test_from_facts_builds_ready_with_db_identity():
    ready, article_id = ReadyForAssessment.from_facts(5, _FACTS)
    assert ready == ReadyForAssessment(
        curation_id=5, translated_title="title", summary="summary"
    )
    assert article_id == 7


@pytest.mark.parametrize(
    "facts, code, article_id",
    [
        (None, AssessmentReadyBuildRejectionReason.CURATION_MISSING, None),
        (
            replace(_FACTS, has_analyzed_article=True),
            AssessmentReadyBuildRejectionReason.ALREADY_IN_SCOPE,
            7,
        ),
        (
            replace(_FACTS, has_out_of_scope_article=True),
            AssessmentReadyBuildRejectionReason.ALREADY_OUT_OF_SCOPE,
            7,
        ),
        (
            replace(_FACTS, has_analyzed_article=True, has_out_of_scope_article=True),
            AssessmentReadyBuildRejectionReason.ALREADY_IN_SCOPE,
            7,
        ),
    ],
)
def test_rejection_conditions_preserve_existing_order_and_article_id(
    facts, code, article_id
):
    rejected = ReadyForAssessment.from_facts(5, facts)
    assert rejected.reason is code
    assert rejected.analyzable_article_id == article_id


@pytest.mark.parametrize(
    "field, value",
    [
        ("translated_title", ""),
        ("summary", ""),
        ("curation_id", 0),
        ("curation_id", -1),
    ],
)
def test_invalid_input_becomes_ready_build_rejection(field, value):
    """入力制約違反をDB由来IDと安全な理由コードへ変換する。"""
    rejected = ReadyForAssessment.from_facts(5, replace(_FACTS, **{field: value}))
    assert rejected.reason is AssessmentReadyBuildRejectionReason.INPUT_INVALID
    assert rejected.analyzable_article_id == 7
    assert rejected.reason.value == "assessment_ready_build_blocked_input_invalid"


@pytest.mark.asyncio
async def test_legacy_entry_loads_once_and_delegates():
    repo = AsyncMock()
    repo.load_ready_build_facts.return_value = _FACTS
    assert await ReadyForAssessment.try_advance_from(
        curation_id=5, repo=repo
    ) == ReadyForAssessment.from_facts(5, _FACTS)
    repo.load_ready_build_facts.assert_awaited_once_with(5)
