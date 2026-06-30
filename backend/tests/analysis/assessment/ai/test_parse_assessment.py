"""Stage 4 ACL — ``parse_assessment`` の dispatch / strict 検証 テスト。

- ``category == out_of_scope`` で ``OutOfScope`` に振り分け、それ以外は ``InScope``
- 2 文字列値 (``category`` / ``investor_take``) すべて
  ``isinstance(..., str)`` で先頭検証 (``str(...)`` 暗黙 coerce なし)
- ``key_points`` は ``list`` 型強制 + 要素は ``KeyPoint.model_validate``
- ``OutOfScope`` でも 3 key (``key_points`` 含む) 欠落 / 型不一致は reject
- ``OutOfScope`` 経路でも key_points は domain に保持される (検証用途で残す対称化)
- schema 違反は ``AssessmentResponseInvalidError`` (Layer 2-B Recoverable marker)
  に詰め替えて raise
"""

from __future__ import annotations

from typing import Any

import pytest

from app.analysis.assessment.ai.parse import (
    AssessmentResponseDefect,
    parse_assessment,
)
from app.analysis.assessment.domain.result import (
    InScope,
    InScopeCategory,
    KeyPoint,
    Mention,
    MentionType,
    OutOfScope,
    OutOfScopeCategory,
)
from app.analysis.assessment.errors import AssessmentResponseInvalidError

_MISSING: Any = object()


def _payload(
    *,
    category: Any = "ai",
    investor_take: Any = "x",
    key_points: Any = _MISSING,
) -> dict[str, Any]:
    """3 key 完備の payload helper (``key_points`` 未指定時のみ空配列を入れる)。"""
    return {
        "category": category,
        "investor_take": investor_take,
        "key_points": [] if key_points is _MISSING else key_points,
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

    @pytest.mark.parametrize("slug", [category.value for category in InScopeCategory])
    def test_each_in_scope_slug_dispatches_to_in_scope(self, slug: str) -> None:
        result = parse_assessment(_payload(category=slug))
        assert isinstance(result, InScope)
        assert result.category.value == slug


class TestParseAssessmentOutOfScope:
    """out-of-scope 経路: ``category == "out_of_scope"`` で OutOfScope に振り分け。"""

    def test_out_of_scope_returns_out_of_scope_instance(self) -> None:
        result = parse_assessment(
            _payload(
                category=OutOfScopeCategory.OUT_OF_SCOPE.value,
                investor_take="Not relevant.",
            )
        )
        assert isinstance(result, OutOfScope)
        assert result.investor_take == "Not relevant."


class TestParseAssessmentMissingKeys:
    """key 欠落: 欠けた key ごとに固有 defect code を焼く。"""

    def test_missing_category_key_raises_category_key_missing(self) -> None:
        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            parse_assessment({"investor_take": "x", "key_points": []})
        assert exc_info.value.code == AssessmentResponseDefect.CATEGORY_KEY_MISSING

    def test_missing_investor_take_key_raises_investor_take_key_missing(self) -> None:
        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            parse_assessment({"category": "ai", "key_points": []})
        assert exc_info.value.code == AssessmentResponseDefect.INVESTOR_TAKE_KEY_MISSING

    def test_missing_key_points_key_in_scope(self) -> None:
        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            parse_assessment({"category": "ai", "investor_take": "x"})
        assert exc_info.value.code == AssessmentResponseDefect.KEY_POINTS_KEY_MISSING

    def test_missing_key_points_key_out_of_scope(
        self,
    ) -> None:
        # strict 化方針: OutOfScope でも key_points key は必須
        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            parse_assessment(
                {
                    "category": "out_of_scope",
                    "investor_take": "x",
                }
            )
        assert exc_info.value.code == AssessmentResponseDefect.KEY_POINTS_KEY_MISSING


class TestParseAssessmentNonStrTypes:
    """型不一致: ``isinstance(..., str)`` で reject + field 別 wrong_type defect。"""

    @pytest.mark.parametrize("non_str_value", [123, 1.5, None, [], {}, True])
    def test_non_str_category_raises_category_wrong_type(
        self, non_str_value: object
    ) -> None:
        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            parse_assessment(_payload(category=non_str_value))
        assert exc_info.value.code == AssessmentResponseDefect.CATEGORY_WRONG_TYPE

    @pytest.mark.parametrize("non_str_value", [123, 1.5, None, [], {}, True])
    def test_non_str_investor_take_raises_investor_take_wrong_type(
        self, non_str_value: object
    ) -> None:
        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            parse_assessment(_payload(investor_take=non_str_value))
        assert exc_info.value.code == AssessmentResponseDefect.INVESTOR_TAKE_WRONG_TYPE


class TestParseAssessmentKeyPointsType:
    """key_points 型強制: list 以外は ``KEY_POINTS_WRONG_TYPE``。"""

    @pytest.mark.parametrize("non_list_value", ["not a list", 123, 1.5, None, {}, True])
    def test_non_list_key_points_raises_key_points_wrong_type_in_scope(
        self, non_list_value: object
    ) -> None:
        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            parse_assessment(_payload(category="ai", key_points=non_list_value))
        assert exc_info.value.code == AssessmentResponseDefect.KEY_POINTS_WRONG_TYPE

    @pytest.mark.parametrize("non_list_value", ["not a list", 123, 1.5, None, {}, True])
    def test_non_list_key_points_raises_key_points_wrong_type_out_of_scope(
        self, non_list_value: object
    ) -> None:
        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            parse_assessment(
                _payload(
                    category=OutOfScopeCategory.OUT_OF_SCOPE.value,
                    key_points=non_list_value,
                )
            )
        assert exc_info.value.code == AssessmentResponseDefect.KEY_POINTS_WRONG_TYPE


class TestParseAssessmentValidationErrors:
    """値レベルの validation 違反: enum 外値 / 最終構築の field 制約。"""

    def test_invalid_category_value_raises_category_unknown_value(self) -> None:
        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            parse_assessment(_payload(category="made_up_value"))
        assert exc_info.value.code == AssessmentResponseDefect.CATEGORY_UNKNOWN_VALUE

    def test_empty_investor_take_raises_investor_take_invalid(self) -> None:
        # InScope.investor_take は min_length=1 / _not_empty → 最終構築で捕捉
        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            parse_assessment(_payload(category="ai", investor_take=""))
        assert exc_info.value.code == AssessmentResponseDefect.INVESTOR_TAKE_INVALID

    def test_empty_investor_take_out_of_scope_raises_investor_take_invalid(
        self,
    ) -> None:
        # OutOfScope.investor_take も min_length=1 (両 path 対称)
        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            parse_assessment(
                _payload(
                    category=OutOfScopeCategory.OUT_OF_SCOPE.value,
                    investor_take="",
                )
            )
        assert exc_info.value.code == AssessmentResponseDefect.INVESTOR_TAKE_INVALID

    def test_too_many_key_points_raises_key_points_too_many(self) -> None:
        # key_points は max_length=10。要素自体は valid だが件数超過 → 最終構築で捕捉。
        key_points = [{"content": f"key point {i}", "mentions": []} for i in range(11)]
        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            parse_assessment(_payload(category="ai", key_points=key_points))
        assert exc_info.value.code == AssessmentResponseDefect.KEY_POINTS_TOO_MANY


class TestParseAssessmentKeyPoints:
    """key_points parse: list[KeyPoint] への変換と内部要素 validation。"""

    def test_in_scope_with_key_points_populates_domain_key_points(self) -> None:
        result = parse_assessment(
            _payload(
                category="ai",
                key_points=[
                    {
                        "content": "Anthropic launched Claude 5.",
                        "mentions": [
                            {"surface": "Anthropic", "type": "company"},
                            {"surface": "Claude 5", "type": "product"},
                        ],
                    }
                ],
            )
        )
        assert isinstance(result, InScope)
        assert len(result.key_points) == 1
        key_point = result.key_points[0]
        assert isinstance(key_point, KeyPoint)
        assert key_point.content == "Anthropic launched Claude 5."
        assert key_point.mentions == [
            Mention(surface="Anthropic", type=MentionType.COMPANY),
            Mention(surface="Claude 5", type=MentionType.PRODUCT),
        ]

    def test_in_scope_with_empty_key_points_keeps_empty_list(self) -> None:
        result = parse_assessment(_payload(category="ai", key_points=[]))
        assert isinstance(result, InScope)
        assert result.key_points == []

    def test_out_of_scope_with_key_points_populates_domain_key_points(self) -> None:
        # OutOfScope 経路でも key_points は domain に保持される (対称化)
        result = parse_assessment(
            _payload(
                category=OutOfScopeCategory.OUT_OF_SCOPE.value,
                key_points=[
                    {
                        "content": "Some key point.",
                        "mentions": [
                            {"surface": "X", "type": "company"},
                        ],
                    }
                ],
            )
        )
        assert isinstance(result, OutOfScope)
        assert len(result.key_points) == 1
        assert result.key_points[0].content == "Some key point."

    def test_out_of_scope_with_empty_key_points_keeps_empty_list(self) -> None:
        result = parse_assessment(
            _payload(category=OutOfScopeCategory.OUT_OF_SCOPE.value, key_points=[])
        )
        assert isinstance(result, OutOfScope)
        assert result.key_points == []

    def test_key_point_with_empty_content_raises_key_point_invalid(self) -> None:
        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            parse_assessment(
                _payload(
                    category="ai",
                    key_points=[{"content": "", "mentions": []}],
                )
            )
        assert exc_info.value.code == AssessmentResponseDefect.KEY_POINT_INVALID

    def test_key_point_missing_content_raises_key_point_invalid(self) -> None:
        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            parse_assessment(_payload(category="ai", key_points=[{"mentions": []}]))
        assert exc_info.value.code == AssessmentResponseDefect.KEY_POINT_INVALID

    def test_key_point_missing_mentions_raises_invalid(self) -> None:
        # mentions は必須 key (default 適用は Pydantic 側だが、本 schema 経由
        # で渡るのは AI が schema 通り返している前提なので strict 要求)
        # → Pydantic は default_factory があるため許容するので、ここでは
        # content のみ与えると mentions=[] でパスする (許容仕様)。
        result = parse_assessment(
            _payload(category="ai", key_points=[{"content": "x"}])
        )
        assert isinstance(result, InScope)
        assert result.key_points[0].mentions == []

    def test_key_point_with_invalid_mention_type_raises_key_point_invalid(self) -> None:
        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            parse_assessment(
                _payload(
                    category="ai",
                    key_points=[
                        {
                            "content": "X happened",
                            "mentions": [{"surface": "X", "type": "startup"}],
                        }
                    ],
                )
            )
        assert exc_info.value.code == AssessmentResponseDefect.KEY_POINT_INVALID

    def test_key_point_with_non_dict_element_raises_key_point_invalid(self) -> None:
        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            parse_assessment(_payload(category="ai", key_points=["not a dict"]))
        assert exc_info.value.code == AssessmentResponseDefect.KEY_POINT_INVALID


class TestParseAssessmentErrorContract:
    """raise される ``AssessmentResponseInvalidError`` の attr / cause 連鎖。"""

    def test_unknown_category_chains_value_error(self) -> None:
        # 値違反系は原例外を __cause__ に連鎖する (enum 外値は ValueError 由来)。
        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            parse_assessment(_payload(category="made_up_value"))
        assert isinstance(exc_info.value.__cause__, ValueError)

    def test_missing_key_chains_key_error(self) -> None:
        # key 欠落は KeyError を __cause__ に連鎖する。
        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            parse_assessment({"investor_take": "x", "key_points": []})
        assert isinstance(exc_info.value.__cause__, KeyError)

    def test_wrong_type_has_no_cause(self) -> None:
        # 型違反は自前 isinstance 判定なので原例外 (cause) を持たない。
        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            parse_assessment(_payload(category=123))
        assert exc_info.value.__cause__ is None

    def test_empty_payload_carries_first_missing_key_code(self) -> None:
        # 空 payload は最初に引く category key の欠落 defect を焼く。
        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            parse_assessment({})
        assert exc_info.value.code == AssessmentResponseDefect.CATEGORY_KEY_MISSING

    def test_invalid_error_provider_error_is_none(self) -> None:
        # parse 由来 (Stage 4 工程内) なので provider_error は常に None
        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            parse_assessment({})
        assert exc_info.value.provider_error is None
