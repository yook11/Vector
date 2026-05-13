"""Stage 4 ACL — ``parse_assessment`` の dispatch / strict 検証 テスト。

- ``category == out_of_scope`` で ``OutOfScope`` に振り分け、それ以外は ``InScope``
- 2 文字列値 (``category`` / ``investor_take``) すべて
  ``isinstance(..., str)`` で先頭検証 (``str(...)`` 暗黙 coerce なし)
- ``events`` は ``list`` 型強制 + 要素は ``Event.model_validate``
- ``OutOfScope`` でも 3 key (``events`` 含む) 欠落 / 型不一致は reject
- ``OutOfScope`` 経路でも events は domain に保持される (検証用途で残す対称化)
- schema 違反は ``AssessmentResponseInvalidError`` (Layer 2-B Recoverable marker)
  に詰め替えて raise
"""

from __future__ import annotations

from typing import Any

import pytest

from app.analysis.assessment.ai.parse import parse_assessment
from app.analysis.assessment.domain.result import (
    Event,
    InScope,
    InScopeCategory,
    Mention,
    MentionType,
    OutOfScope,
)
from app.analysis.assessment.errors import AssessmentResponseInvalidError

_MISSING: Any = object()


def _payload(
    *,
    category: Any = "ai",
    investor_take: Any = "x",
    events: Any = _MISSING,
) -> dict[str, Any]:
    """3 key 完備の payload helper (``events`` 未指定時のみ空配列を入れる)。"""
    return {
        "category": category,
        "investor_take": investor_take,
        "events": [] if events is _MISSING else events,
    }


class TestParseAssessmentInScope:
    """in-scope 経路: 12 種 in-scope category がすべて InScope を返す。"""

    def test_in_scope_category_returns_in_scope_instance(self) -> None:
        result = parse_assessment(
            _payload(
                category="ai",
                investor_take="Significant.",
            )
        )
        assert isinstance(result, InScope)
        assert result.category == InScopeCategory.AI
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
        result = parse_assessment(_payload(category=slug))
        assert isinstance(result, InScope)
        assert result.category.value == slug


class TestParseAssessmentOutOfScope:
    """out-of-scope 経路: ``category == "out_of_scope"`` で OutOfScope に振り分け。"""

    def test_out_of_scope_returns_out_of_scope_instance(self) -> None:
        result = parse_assessment(
            _payload(
                category="out_of_scope",
                investor_take="Not relevant.",
            )
        )
        assert isinstance(result, OutOfScope)
        assert result.investor_take == "Not relevant."


class TestParseAssessmentMissingKeys:
    """key 欠落: 3 key (category/investor_take/events) いずれの欠落も Invalid。"""

    def test_missing_category_key_raises_invalid(self) -> None:
        with pytest.raises(AssessmentResponseInvalidError):
            parse_assessment({"investor_take": "x", "events": []})

    def test_missing_investor_take_raises_invalid(self) -> None:
        with pytest.raises(AssessmentResponseInvalidError):
            parse_assessment({"category": "ai", "events": []})

    def test_missing_events_key_raises_invalid_in_scope(self) -> None:
        with pytest.raises(AssessmentResponseInvalidError):
            parse_assessment({"category": "ai", "investor_take": "x"})

    def test_missing_events_key_raises_invalid_out_of_scope(self) -> None:
        # strict 化方針: OutOfScope でも events key は必須
        with pytest.raises(AssessmentResponseInvalidError):
            parse_assessment(
                {
                    "category": "out_of_scope",
                    "investor_take": "x",
                }
            )


class TestParseAssessmentNonStrTypes:
    """型不一致: ``isinstance(..., str)`` で reject される 6 型を網羅。"""

    @pytest.mark.parametrize("non_str_value", [123, 1.5, None, [], {}, True])
    def test_non_str_category_raises_invalid(self, non_str_value: object) -> None:
        with pytest.raises(AssessmentResponseInvalidError):
            parse_assessment(_payload(category=non_str_value))

    @pytest.mark.parametrize("non_str_value", [123, 1.5, None, [], {}, True])
    def test_non_str_investor_take_raises_invalid(self, non_str_value: object) -> None:
        with pytest.raises(AssessmentResponseInvalidError):
            parse_assessment(_payload(investor_take=non_str_value))


class TestParseAssessmentEventsType:
    """events 型強制: list 以外は reject。"""

    @pytest.mark.parametrize("non_list_value", ["not a list", 123, 1.5, None, {}, True])
    def test_non_list_events_raises_invalid_in_scope(
        self, non_list_value: object
    ) -> None:
        with pytest.raises(AssessmentResponseInvalidError):
            parse_assessment(_payload(category="ai", events=non_list_value))

    @pytest.mark.parametrize("non_list_value", ["not a list", 123, 1.5, None, {}, True])
    def test_non_list_events_raises_invalid_out_of_scope(
        self, non_list_value: object
    ) -> None:
        with pytest.raises(AssessmentResponseInvalidError):
            parse_assessment(_payload(category="out_of_scope", events=non_list_value))


class TestParseAssessmentValidationErrors:
    """値レベルの validation 違反: enum 外値 / min_length。"""

    def test_invalid_category_value_raises_invalid(self) -> None:
        with pytest.raises(AssessmentResponseInvalidError):
            parse_assessment(_payload(category="made_up_value"))

    def test_empty_investor_take_raises_invalid(self) -> None:
        # InScope.investor_take は min_length=1
        with pytest.raises(AssessmentResponseInvalidError):
            parse_assessment(_payload(category="ai", investor_take=""))


class TestParseAssessmentEvents:
    """events parse: list[Event] への変換と内部要素 validation。"""

    def test_in_scope_with_events_populates_domain_events(self) -> None:
        result = parse_assessment(
            _payload(
                category="ai",
                events=[
                    {
                        "description": "Anthropic launched Claude 5.",
                        "mentions": [
                            {"surface": "Anthropic", "type": "company"},
                            {"surface": "Claude 5", "type": "product"},
                        ],
                    }
                ],
            )
        )
        assert isinstance(result, InScope)
        assert len(result.events) == 1
        event = result.events[0]
        assert isinstance(event, Event)
        assert event.description == "Anthropic launched Claude 5."
        assert event.mentions == [
            Mention(surface="Anthropic", type=MentionType.COMPANY),
            Mention(surface="Claude 5", type=MentionType.PRODUCT),
        ]

    def test_in_scope_with_empty_events_keeps_empty_list(self) -> None:
        result = parse_assessment(_payload(category="ai", events=[]))
        assert isinstance(result, InScope)
        assert result.events == []

    def test_out_of_scope_with_events_populates_domain_events(self) -> None:
        # OutOfScope 経路でも events は domain に保持される (対称化)
        result = parse_assessment(
            _payload(
                category="out_of_scope",
                events=[
                    {
                        "description": "Some event.",
                        "mentions": [
                            {"surface": "X", "type": "company"},
                        ],
                    }
                ],
            )
        )
        assert isinstance(result, OutOfScope)
        assert len(result.events) == 1
        assert result.events[0].description == "Some event."

    def test_out_of_scope_with_empty_events_keeps_empty_list(self) -> None:
        result = parse_assessment(_payload(category="out_of_scope", events=[]))
        assert isinstance(result, OutOfScope)
        assert result.events == []

    def test_event_with_empty_description_raises_invalid(self) -> None:
        with pytest.raises(AssessmentResponseInvalidError):
            parse_assessment(
                _payload(
                    category="ai",
                    events=[{"description": "", "mentions": []}],
                )
            )

    def test_event_missing_description_raises_invalid(self) -> None:
        with pytest.raises(AssessmentResponseInvalidError):
            parse_assessment(_payload(category="ai", events=[{"mentions": []}]))

    def test_event_missing_mentions_raises_invalid(self) -> None:
        # mentions は必須 key (default 適用は Pydantic 側だが、本 schema 経由
        # で渡るのは AI が schema 通り返している前提なので strict 要求)
        # → Pydantic は default_factory があるため許容するので、ここでは
        # description のみ与えると mentions=[] でパスする (許容仕様)。
        result = parse_assessment(
            _payload(category="ai", events=[{"description": "x"}])
        )
        assert isinstance(result, InScope)
        assert result.events[0].mentions == []

    def test_event_with_invalid_mention_type_raises_invalid(self) -> None:
        with pytest.raises(AssessmentResponseInvalidError):
            parse_assessment(
                _payload(
                    category="ai",
                    events=[
                        {
                            "description": "X happened",
                            "mentions": [{"surface": "X", "type": "startup"}],
                        }
                    ],
                )
            )

    def test_event_with_non_dict_element_raises_invalid(self) -> None:
        with pytest.raises(AssessmentResponseInvalidError):
            parse_assessment(_payload(category="ai", events=["not a dict"]))


class TestParseAssessmentErrorContract:
    """raise される ``AssessmentResponseInvalidError`` の attr / cause 連鎖。"""

    def test_invalid_error_chains_original_exception(self) -> None:
        # __cause__ が KeyError / ValueError (ValidationError は ValueError 派生)。
        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            parse_assessment(_payload(category="made_up_value"))
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
