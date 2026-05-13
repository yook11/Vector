"""Stage 4 ``AssessmentCall`` envelope の構造テスト (frozen / slots / 5 field)。"""

from __future__ import annotations

import dataclasses

import pytest

from app.analysis.assessment.ai.envelope import AssessmentCall
from app.analysis.assessment.domain.result import (
    InScope,
    InScopeCategory,
    OutOfScope,
)


def _make_in_scope() -> InScope:
    return InScope(
        category=InScopeCategory.AI,
        investor_take="x",
    )


class TestAssessmentCallConstruction:
    """5 field をすべて受け取って構築される。"""

    def test_construction_with_in_scope_result(self) -> None:
        result = _make_in_scope()
        call = AssessmentCall(
            result=result,
            raw_response='{"category": "ai", ...}',
            raw_category="ai",
            prompt_version="abc12345",
            model_name="test-model",
        )
        assert call.result is result
        assert call.raw_response.startswith('{"category"')
        assert call.raw_category == "ai"
        assert call.prompt_version == "abc12345"
        assert call.model_name == "test-model"

    def test_construction_with_out_of_scope_result(self) -> None:
        result = OutOfScope(investor_take="x")
        call = AssessmentCall(
            result=result,
            raw_response='{"category": "out_of_scope", ...}',
            raw_category="out_of_scope",
            prompt_version="abc12345",
            model_name="test-model",
        )
        assert isinstance(call.result, OutOfScope)


class TestAssessmentCallImmutability:
    """frozen=True + slots=True の structural 保証。"""

    def test_is_frozen(self) -> None:
        call = AssessmentCall(
            result=OutOfScope(investor_take="x"),
            raw_response="r",
            raw_category="c",
            prompt_version="v",
            model_name="m",
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            call.raw_response = "mutated"  # type: ignore[misc]

    def test_uses_slots(self) -> None:
        call = AssessmentCall(
            result=OutOfScope(investor_take="x"),
            raw_response="r",
            raw_category="c",
            prompt_version="v",
            model_name="m",
        )
        # slots=True により instance __dict__ が無く、未定義 attr の追加も拒否
        assert not hasattr(call, "__dict__")
