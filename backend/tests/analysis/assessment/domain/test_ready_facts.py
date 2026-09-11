"""取得済みの開始条件と既存の非同期入口の契約を検証する。"""

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.analysis.assessment.domain.ready import (
    AssessmentReadyBuildBlockedCode,
    AssessmentReadyBuildBlockedError,
    AssessmentReadyBuildFacts,
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
        (None, AssessmentReadyBuildBlockedCode.CURATION_MISSING, None),
        (
            replace(_FACTS, has_analyzed_article=True),
            AssessmentReadyBuildBlockedCode.ALREADY_IN_SCOPE,
            7,
        ),
        (
            replace(_FACTS, has_out_of_scope_article=True),
            AssessmentReadyBuildBlockedCode.ALREADY_OUT_OF_SCOPE,
            7,
        ),
        (
            replace(_FACTS, has_analyzed_article=True, has_out_of_scope_article=True),
            AssessmentReadyBuildBlockedCode.ALREADY_IN_SCOPE,
            7,
        ),
    ],
)
def test_blocked_conditions_preserve_existing_order_and_article_id(
    facts, code, article_id
):
    with pytest.raises(AssessmentReadyBuildBlockedError) as raised:
        ReadyForAssessment.from_facts(5, facts)
    assert raised.value.code is code
    assert raised.value.analyzable_article_id == article_id


def test_invalid_input_remains_validation_error():
    with pytest.raises(ValidationError):
        ReadyForAssessment.from_facts(5, replace(_FACTS, summary=""))


@pytest.mark.asyncio
async def test_legacy_entry_loads_once_and_delegates():
    repo = AsyncMock()
    repo.load_ready_build_facts.return_value = _FACTS
    assert await ReadyForAssessment.try_advance_from(
        curation_id=5, repo=repo
    ) == ReadyForAssessment.from_facts(5, _FACTS)
    repo.load_ready_build_facts.assert_awaited_once_with(5)
