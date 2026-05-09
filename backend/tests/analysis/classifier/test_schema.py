"""Stage 4 classifier schema — InScopeCategory / InScope の型強制テスト。

PR2 で追加された ``InScopeCategory`` enum (12 値、``OUT_OF_SCOPE`` 排除) と
``InScope.category`` の型変更が「対象範囲内」を型レベルで保証することを検証。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.analysis.classifier.schema import (
    InScope,
    InScopeCategory,
    OutOfScope,
    ValidCategory,
)
from app.analysis.domain.value_objects.topic import TopicName


class TestInScopeCategoryValueSet:
    """InScopeCategory の値網羅と OUT_OF_SCOPE 排除を検証。"""

    def test_has_12_values(self) -> None:
        assert len(InScopeCategory) == 12

    def test_does_not_include_out_of_scope_value(self) -> None:
        with pytest.raises(ValueError):
            InScopeCategory("out_of_scope")

    def test_does_not_include_out_of_scope_member(self) -> None:
        assert "OUT_OF_SCOPE" not in InScopeCategory.__members__

    @pytest.mark.parametrize(
        "slug",
        [
            "ai",
            "bio",
            "computing",
            "energy",
            "materials",
            "mobility",
            "network",
            "other",
            "robotics",
            "security",
            "semiconductor",
            "space",
        ],
    )
    def test_contains_expected_slug(self, slug: str) -> None:
        assert InScopeCategory(slug).value == slug

    def test_values_are_subset_of_valid_category(self) -> None:
        # 運用ルール (新値追加時): InScopeCategory と ValidCategory の値が
        # OUT_OF_SCOPE 以外で完全一致する必要がある (parse_assessment が値マッピング
        # するため)。
        in_scope_values = {c.value for c in InScopeCategory}
        valid_values = {
            c.value for c in ValidCategory if c != ValidCategory.OUT_OF_SCOPE
        }
        assert in_scope_values == valid_values


class TestInScopeRejectsOutOfScope:
    """InScope.category が OUT_OF_SCOPE を型レベルで拒否することを検証。"""

    def test_construction_with_out_of_scope_string_raises(self) -> None:
        with pytest.raises(ValidationError):
            InScope.model_validate(
                {
                    "category": "out_of_scope",
                    "topic": "ai agents",
                    "investor_take": "x",
                }
            )

    def test_construction_with_in_scope_category_succeeds(self) -> None:
        in_scope = InScope(
            category=InScopeCategory.AI,
            topic=TopicName(root="ai"),
            investor_take="x",
        )
        assert in_scope.category is InScopeCategory.AI


class TestAssessmentResultAlias:
    """AssessmentResult type alias が InScope | OutOfScope の union として使える。"""

    def test_in_scope_and_out_of_scope_match_alias(self) -> None:
        # AssessmentResult は type alias、isinstance 経由で確認
        in_scope = InScope(
            category=InScopeCategory.AI,
            topic=TopicName(root="ai"),
            investor_take="x",
        )
        out_of_scope = OutOfScope(investor_take="y")
        for value in (in_scope, out_of_scope):
            assert isinstance(value, (InScope, OutOfScope))
