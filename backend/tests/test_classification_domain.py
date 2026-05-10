"""assessment ドメイン層のユニットテスト (DB 不要)。

Entity (``InScopeAssessment`` / ``OutOfScopeAssessment``) の ``__post_init__``
不変条件を検証する。

AI 境界型 (``InScope`` / ``OutOfScope``) の sanitize / 長さ上限は
``tests/analysis/classifier/test_schema.py`` が保護網を持つ。

注 (PR3.5-d.0): ファイル名 ``test_classification_domain.py`` は本 PR で
rename しない (別 cleanup PR で ``test_assessment_domain.py`` に rename
予定)。内容は assessment 命名に追従済。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.analysis.assessment.domain.in_scope import InScopeAssessment
from app.analysis.assessment.domain.out_of_scope import OutOfScopeAssessment
from app.analysis.domain.value_objects.topic import TopicName

# ---------------------------------------------------------------------------
# InScopeAssessment Entity — __post_init__
# ---------------------------------------------------------------------------


def _make_assessment(**overrides: object) -> InScopeAssessment:
    defaults: dict[str, object] = {
        "id": 1,
        "extraction_id": 2,
        "translated_title": "title",
        "summary": "summary",
        "topic": TopicName(root="ai agents"),
        "category_id": 3,
        "investor_take": "reason",
        "ai_model": "gemini-2.5-pro",
        "analyzed_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    defaults.update(overrides)
    return InScopeAssessment(**defaults)  # type: ignore[arg-type]


class TestInScopeAssessmentPostInit:
    def test_constructs_with_valid_args(self) -> None:
        assessment = _make_assessment()
        assert assessment.id == 1

    @pytest.mark.parametrize(
        "field",
        ["translated_title", "summary", "investor_take", "ai_model"],
    )
    def test_rejects_empty_string_fields(self, field: str) -> None:
        with pytest.raises(ValueError):
            _make_assessment(**{field: ""})

    @pytest.mark.parametrize("field", ["id", "extraction_id", "category_id"])
    @pytest.mark.parametrize("value", [0, -1])
    def test_rejects_non_positive_identifiers(self, field: str, value: int) -> None:
        with pytest.raises(ValueError):
            _make_assessment(**{field: value})

    def test_is_frozen(self) -> None:
        assessment = _make_assessment()
        with pytest.raises((AttributeError, TypeError)):
            assessment.id = 999  # type: ignore[misc]


# ---------------------------------------------------------------------------
# OutOfScopeAssessment Entity — __post_init__
# ---------------------------------------------------------------------------


def _make_out_of_scope_assessment(**overrides: object) -> OutOfScopeAssessment:
    defaults: dict[str, object] = {
        "id": 1,
        "extraction_id": 2,
        "investor_take": "out of scope",
        "ai_model": "gemini-2.5-pro",
        "rejected_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    defaults.update(overrides)
    return OutOfScopeAssessment(**defaults)  # type: ignore[arg-type]


class TestOutOfScopeAssessmentPostInit:
    def test_constructs_with_valid_args(self) -> None:
        assessment = _make_out_of_scope_assessment()
        assert assessment.id == 1

    @pytest.mark.parametrize("field", ["investor_take", "ai_model"])
    def test_rejects_empty_string_fields(self, field: str) -> None:
        with pytest.raises(ValueError):
            _make_out_of_scope_assessment(**{field: ""})

    @pytest.mark.parametrize("field", ["id", "extraction_id"])
    @pytest.mark.parametrize("value", [0, -1])
    def test_rejects_non_positive_identifiers(self, field: str, value: int) -> None:
        with pytest.raises(ValueError):
            _make_out_of_scope_assessment(**{field: value})

    def test_is_frozen(self) -> None:
        assessment = _make_out_of_scope_assessment()
        with pytest.raises((AttributeError, TypeError)):
            assessment.id = 999  # type: ignore[misc]
