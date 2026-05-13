"""Stage 4 ドメイン結果型 — ``InScopeCategory`` / ``InScope`` / ``OutOfScope`` /
``ValidCategory`` の型強制テスト。

``InScopeCategory`` enum (12 値、``OUT_OF_SCOPE`` 排除) と ``InScope.category``
の型が「対象範囲内」を型レベルで保証することを検証する。AI 境界での sanitize +
bounds 保護 (BC 境界原則) も併せて固定する。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.analysis.assessment.domain.result import (
    Event,
    InScope,
    InScopeCategory,
    Mention,
    MentionType,
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


class TestInScopeInvestorTakeSanitize:
    """InScope.investor_take の sanitize + bounds 保護網 (AI 境界 BC 責務)。"""

    def test_strips_html_tags(self) -> None:
        m = InScope(
            category=InScopeCategory.AI,
            topic=TopicName(root="ai"),
            investor_take="<b>note</b>",
        )
        assert m.investor_take == "note"

    def test_strips_control_characters(self) -> None:
        m = InScope(
            category=InScopeCategory.AI,
            topic=TopicName(root="ai"),
            investor_take="note\x00with\x07control",
        )
        assert "\x00" not in m.investor_take
        assert "\x07" not in m.investor_take

    def test_nfkc_normalizes_fullwidth(self) -> None:
        m = InScope(
            category=InScopeCategory.AI,
            topic=TopicName(root="ai"),
            investor_take="ABC123",  # fullwidth
        )
        assert m.investor_take == "ABC123"

    def test_rejects_empty_after_sanitization(self) -> None:
        with pytest.raises(ValidationError):
            InScope(
                category=InScopeCategory.AI,
                topic=TopicName(root="ai"),
                investor_take="<i></i>",
            )

    def test_rejects_over_max_length(self) -> None:
        with pytest.raises(ValidationError):
            InScope(
                category=InScopeCategory.AI,
                topic=TopicName(root="ai"),
                investor_take="a" * 2001,
            )

    def test_accepts_max_length_boundary(self) -> None:
        m = InScope(
            category=InScopeCategory.AI,
            topic=TopicName(root="ai"),
            investor_take="a" * 2000,
        )
        assert len(m.investor_take) == 2000


class TestOutOfScopeInvestorTakeSanitize:
    """OutOfScope.investor_take の sanitize + bounds 保護網 (InScope と対称)。"""

    def test_strips_html_and_control_chars(self) -> None:
        m = OutOfScope(investor_take="<b>off-topic</b>\x00 article")
        assert "<" not in m.investor_take
        assert "\x00" not in m.investor_take

    def test_rejects_empty_after_sanitization(self) -> None:
        with pytest.raises(ValidationError):
            OutOfScope(investor_take="<i></i>")

    def test_rejects_over_max_length(self) -> None:
        with pytest.raises(ValidationError):
            OutOfScope(investor_take="a" * 2001)

    def test_accepts_max_length_boundary(self) -> None:
        m = OutOfScope(investor_take="a" * 2000)
        assert len(m.investor_take) == 2000


class TestMentionTypeValueSet:
    """MentionType の値網羅 (6 軸網羅 + enum 外値 reject)。"""

    def test_has_6_values(self) -> None:
        assert len(MentionType) == 6

    @pytest.mark.parametrize(
        "value",
        ["company", "government", "academic", "product", "technology", "person"],
    )
    def test_contains_expected_value(self, value: str) -> None:
        assert MentionType(value).value == value

    def test_rejects_unknown_value(self) -> None:
        with pytest.raises(ValueError):
            MentionType("startup")


class TestMentionSanitize:
    """Mention.surface の sanitize + bounds (NFKC + 空白整形のみ、casing は保持)。"""

    def test_preserves_casing(self) -> None:
        # AI 抽出 casing は文脈情報、lower 化しない (feedback_ai_extraction_casing)
        m = Mention(surface="OpenAI", type=MentionType.COMPANY)
        assert m.surface == "OpenAI"

    def test_strips_html_tags(self) -> None:
        m = Mention(surface="<b>NVIDIA</b>", type=MentionType.COMPANY)
        assert m.surface == "NVIDIA"

    def test_nfkc_normalizes_fullwidth(self) -> None:
        m = Mention(surface="NVIDIA", type=MentionType.COMPANY)
        assert m.surface == "NVIDIA"

    def test_rejects_empty_after_sanitization(self) -> None:
        with pytest.raises(ValidationError):
            Mention(surface="<i></i>", type=MentionType.COMPANY)

    def test_rejects_over_max_length(self) -> None:
        with pytest.raises(ValidationError):
            Mention(surface="a" * 201, type=MentionType.COMPANY)

    def test_accepts_max_length_boundary(self) -> None:
        m = Mention(surface="a" * 200, type=MentionType.COMPANY)
        assert len(m.surface) == 200

    def test_rejects_unknown_mention_type(self) -> None:
        with pytest.raises(ValidationError):
            Mention.model_validate({"surface": "x", "type": "startup"})

    def test_is_frozen(self) -> None:
        m = Mention(surface="x", type=MentionType.COMPANY)
        with pytest.raises(ValidationError):
            m.surface = "y"  # type: ignore[misc]


class TestEventSanitize:
    """Event.description の sanitize + Event.mentions のデフォルト/上限。"""

    def test_strips_html_tags_in_description(self) -> None:
        e = Event(description="<b>X announced Y</b>")
        assert e.description == "X announced Y"

    def test_rejects_empty_description_after_sanitization(self) -> None:
        with pytest.raises(ValidationError):
            Event(description="<i></i>")

    def test_rejects_over_max_description_length(self) -> None:
        with pytest.raises(ValidationError):
            Event(description="a" * 501)

    def test_accepts_max_description_length_boundary(self) -> None:
        e = Event(description="a" * 500)
        assert len(e.description) == 500

    def test_mentions_defaults_to_empty_list(self) -> None:
        e = Event(description="X happened")
        assert e.mentions == []

    def test_accepts_multiple_mentions(self) -> None:
        e = Event(
            description="X announced Y",
            mentions=[
                Mention(surface="OpenAI", type=MentionType.COMPANY),
                Mention(surface="GPT-5", type=MentionType.PRODUCT),
            ],
        )
        assert len(e.mentions) == 2

    def test_rejects_over_max_mentions(self) -> None:
        too_many = [
            Mention(surface=f"company-{i}", type=MentionType.COMPANY) for i in range(21)
        ]
        with pytest.raises(ValidationError):
            Event(description="X happened", mentions=too_many)

    def test_is_frozen(self) -> None:
        e = Event(description="X happened")
        with pytest.raises(ValidationError):
            e.description = "Y"  # type: ignore[misc]


class TestInScopeEvents:
    """InScope.events の追加フィールド (PR 1 並列運用中は空配列許容)。"""

    def test_events_defaults_to_empty_list(self) -> None:
        # PR 1 並列運用中は既存 fixture / AI が events を返さないケースを許容
        in_scope = InScope(
            category=InScopeCategory.AI,
            topic=TopicName(root="ai"),
            investor_take="x",
        )
        assert in_scope.events == []

    def test_accepts_events_list(self) -> None:
        in_scope = InScope(
            category=InScopeCategory.AI,
            topic=TopicName(root="ai"),
            investor_take="x",
            events=[
                Event(
                    description="X announced Y",
                    mentions=[Mention(surface="X", type=MentionType.COMPANY)],
                )
            ],
        )
        assert len(in_scope.events) == 1
        assert in_scope.events[0].description == "X announced Y"

    def test_rejects_over_max_events(self) -> None:
        too_many = [Event(description=f"event {i}") for i in range(11)]
        with pytest.raises(ValidationError):
            InScope(
                category=InScopeCategory.AI,
                topic=TopicName(root="ai"),
                investor_take="x",
                events=too_many,
            )


class TestOutOfScopeEvents:
    """OutOfScope.events の追加フィールド (InScope と対称、空配列許容)。"""

    def test_events_defaults_to_empty_list(self) -> None:
        out_of_scope = OutOfScope(investor_take="x")
        assert out_of_scope.events == []

    def test_accepts_events_list(self) -> None:
        out_of_scope = OutOfScope(
            investor_take="x",
            events=[
                Event(
                    description="Y happened",
                    mentions=[Mention(surface="Z", type=MentionType.PRODUCT)],
                )
            ],
        )
        assert len(out_of_scope.events) == 1
        assert out_of_scope.events[0].description == "Y happened"

    def test_rejects_over_max_events(self) -> None:
        too_many = [Event(description=f"event {i}") for i in range(11)]
        with pytest.raises(ValidationError):
            OutOfScope(investor_take="x", events=too_many)
