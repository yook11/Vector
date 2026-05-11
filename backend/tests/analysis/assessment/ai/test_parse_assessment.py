"""Stage 4 ACL — ``parse_assessment`` の dispatch / strict 検証 テスト。

PR2 で追加された ``parse_assessment`` 関数が:
- ``category == out_of_scope`` で ``OutOfScope`` に振り分け、それ以外は ``InScope``
- 3 値 (``category`` / ``topic`` / ``investor_take``) すべて ``isinstance(..., str)``
  で先頭検証 (``str(...)`` 暗黙 coerce なし)
- ``OutOfScope`` でも ``topic`` key 欠落 / 非 str は reject、ただし ``topic=""`` や
  VO 正規化違反 (4 語等) は通す
- schema 違反は ``AssessmentResponseInvalidError`` (Layer 2-B Recoverable marker)
  に詰め替えて raise

を検証する。
"""

from __future__ import annotations

import pytest

from app.analysis.assessment.ai.parse import parse_assessment
from app.analysis.assessment.domain.result import (
    InScope,
    InScopeCategory,
    OutOfScope,
)
from app.analysis.assessment.errors import AssessmentResponseInvalidError


class TestParseAssessmentInScope:
    """in-scope 経路: 12 種 in-scope category がすべて InScope を返す。"""

    def test_in_scope_category_returns_in_scope_instance(self) -> None:
        result = parse_assessment(
            {
                "category": "ai",
                "topic": "ai agents",
                "investor_take": "Significant.",
            }
        )
        assert isinstance(result, InScope)
        assert result.category == InScopeCategory.AI
        assert result.topic.root == "ai agents"
        assert result.investor_take == "Significant."

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
    def test_each_in_scope_slug_dispatches_to_in_scope(self, slug: str) -> None:
        result = parse_assessment(
            {
                "category": slug,
                "topic": "ai",
                "investor_take": "x",
            }
        )
        assert isinstance(result, InScope)
        assert result.category.value == slug


class TestParseAssessmentOutOfScope:
    """out-of-scope 経路: ``category == "out_of_scope"`` で OutOfScope に振り分け。"""

    def test_out_of_scope_returns_out_of_scope_instance(self) -> None:
        result = parse_assessment(
            {
                "category": "out_of_scope",
                "topic": "ignored",
                "investor_take": "Not relevant.",
            }
        )
        assert isinstance(result, OutOfScope)
        assert result.investor_take == "Not relevant."

    def test_out_of_scope_accepts_arbitrary_topic_string(self) -> None:
        # OutOfScope では topic 値の VO 正規化は適用しない。raw str なら受理
        # (4 語 / stopword 含み等もそのまま通す、parse 結果には残らない)。
        result = parse_assessment(
            {
                "category": "out_of_scope",
                "topic": "anything goes here even 4 word topic",
                "investor_take": "x",
            }
        )
        assert isinstance(result, OutOfScope)

    def test_out_of_scope_accepts_empty_string_topic(self) -> None:
        # OutOfScope では topic="" も raw str として通す (TopicName 制約なし)。
        result = parse_assessment(
            {
                "category": "out_of_scope",
                "topic": "",
                "investor_take": "x",
            }
        )
        assert isinstance(result, OutOfScope)


class TestParseAssessmentMissingKeys:
    """key 欠落: 3 key (category / topic / investor_take) いずれの欠落も Invalid。"""

    def test_missing_category_key_raises_invalid(self) -> None:
        with pytest.raises(AssessmentResponseInvalidError):
            parse_assessment({"topic": "ai", "investor_take": "x"})

    def test_missing_topic_key_raises_invalid_in_scope(self) -> None:
        with pytest.raises(AssessmentResponseInvalidError):
            parse_assessment({"category": "ai", "investor_take": "x"})

    def test_missing_topic_key_raises_invalid_out_of_scope(self) -> None:
        # strict 化方針: OutOfScope でも topic key は必須
        with pytest.raises(AssessmentResponseInvalidError):
            parse_assessment(
                {
                    "category": "out_of_scope",
                    "investor_take": "x",
                }
            )

    def test_missing_investor_take_raises_invalid(self) -> None:
        with pytest.raises(AssessmentResponseInvalidError):
            parse_assessment({"category": "ai", "topic": "ai"})


class TestParseAssessmentNonStrTypes:
    """型不一致: ``isinstance(..., str)`` で reject される 6 型を網羅。"""

    @pytest.mark.parametrize("non_str_value", [123, 1.5, None, [], {}, True])
    def test_non_str_category_raises_invalid(self, non_str_value: object) -> None:
        with pytest.raises(AssessmentResponseInvalidError):
            parse_assessment(
                {
                    "category": non_str_value,
                    "topic": "ai",
                    "investor_take": "x",
                }
            )

    @pytest.mark.parametrize("non_str_value", [123, 1.5, None, [], {}, True])
    def test_non_str_topic_raises_invalid_in_scope(self, non_str_value: object) -> None:
        with pytest.raises(AssessmentResponseInvalidError):
            parse_assessment(
                {
                    "category": "ai",
                    "topic": non_str_value,
                    "investor_take": "x",
                }
            )

    @pytest.mark.parametrize("non_str_value", [123, 1.5, None, [], {}, True])
    def test_non_str_topic_raises_invalid_out_of_scope(
        self, non_str_value: object
    ) -> None:
        # strict 化方針: OutOfScope でも topic は str 型強制
        with pytest.raises(AssessmentResponseInvalidError):
            parse_assessment(
                {
                    "category": "out_of_scope",
                    "topic": non_str_value,
                    "investor_take": "x",
                }
            )

    @pytest.mark.parametrize("non_str_value", [123, 1.5, None, [], {}, True])
    def test_non_str_investor_take_raises_invalid(self, non_str_value: object) -> None:
        with pytest.raises(AssessmentResponseInvalidError):
            parse_assessment(
                {
                    "category": "ai",
                    "topic": "ai",
                    "investor_take": non_str_value,
                }
            )


class TestParseAssessmentValidationErrors:
    """値レベルの validation 違反: enum 外値 / TopicName 制約 / min_length。"""

    def test_invalid_category_value_raises_invalid(self) -> None:
        with pytest.raises(AssessmentResponseInvalidError):
            parse_assessment(
                {
                    "category": "made_up_value",
                    "topic": "ai",
                    "investor_take": "x",
                }
            )

    def test_in_scope_4_word_topic_raises_invalid(self) -> None:
        # TopicName が 4 語拒否
        with pytest.raises(AssessmentResponseInvalidError):
            parse_assessment(
                {
                    "category": "ai",
                    "topic": "one two three four",
                    "investor_take": "x",
                }
            )

    def test_in_scope_empty_topic_raises_invalid(self) -> None:
        # InScope では TopicName VO の min_length=2 制約が効く
        with pytest.raises(AssessmentResponseInvalidError):
            parse_assessment(
                {
                    "category": "ai",
                    "topic": "",
                    "investor_take": "x",
                }
            )

    def test_empty_investor_take_raises_invalid(self) -> None:
        # InScope.investor_take は min_length=1
        with pytest.raises(AssessmentResponseInvalidError):
            parse_assessment(
                {
                    "category": "ai",
                    "topic": "ai",
                    "investor_take": "",
                }
            )


class TestParseAssessmentErrorContract:
    """raise される ``AssessmentResponseInvalidError`` の attr / cause 連鎖。"""

    def test_invalid_error_chains_original_exception(self) -> None:
        # __cause__ が KeyError / ValueError (ValidationError は ValueError 派生)。
        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            parse_assessment(
                {
                    "category": "made_up_value",
                    "topic": "ai",
                    "investor_take": "x",
                }
            )
        assert exc_info.value.__cause__ is not None
        assert isinstance(exc_info.value.__cause__, (KeyError, ValueError))

    def test_invalid_error_carries_correct_code(self) -> None:
        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            parse_assessment({})
        assert exc_info.value.code == "assessment_response_invalid"

    def test_invalid_error_provider_error_is_none(self) -> None:
        # Layer 2-B (Stage 4 工程由来) なので provider_error は常に None
        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            parse_assessment({})
        assert exc_info.value.provider_error is None
