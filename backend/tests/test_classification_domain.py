"""assessment ドメイン層のユニットテスト (DB 不要)。

Draft の sanitize / validation、Entity の __post_init__、
ファクトリ (from_in_scope / from_out_of_scope) を検証する。

注 (PR3.5-d.0): ファイル名 ``test_classification_domain.py`` は本 PR で
rename しない (別 cleanup PR で ``test_assessment_domain.py`` に rename
予定)。内容は assessment 命名に追従済。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.analysis.assessment.domain.in_scope import (
    InScopeAssessment,
    InScopeAssessmentDraft,
)
from app.analysis.assessment.domain.out_of_scope import (
    OutOfScopeAssessment,
    OutOfScopeAssessmentDraft,
)
from app.analysis.classifier.schema import InScope, OutOfScope, ValidCategory
from app.analysis.domain.value_objects.topic import TopicName


def _make_in_scope(**overrides: object) -> InScope:
    defaults: dict[str, object] = {
        "category": ValidCategory.AI,
        "topic": TopicName(root="ai agents"),
        "investor_take": "Significant advancement in agent autonomy.",
    }
    defaults.update(overrides)
    return InScope(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# InScopeAssessmentDraft — sanitize / validation
# ---------------------------------------------------------------------------


class TestInScopeAssessmentDraftSanitize:
    def test_strips_html_tags_from_title_summary_investor_take(self) -> None:
        draft = InScopeAssessmentDraft(
            translated_title="<b>Title</b>",
            summary="<p>Summary <i>here</i></p>",
            topic_name=TopicName(root="ai agents"),
            investor_take="<script>bad()</script>Reason",
        )
        assert draft.translated_title == "Title"
        assert draft.summary == "Summary here"
        assert draft.investor_take == "bad()Reason"

    def test_strips_c0_c1_control_chars(self) -> None:
        # C0 (\x00-\x1f, タブ/改行除く) と C1 (\x7f-\x9f) は除去対象。
        draft = InScopeAssessmentDraft(
            translated_title="title\x00x",
            summary="ok\x01summary",
            topic_name=TopicName(root="ai"),
            investor_take="reason\x7fok",
        )
        assert "\x00" not in draft.translated_title
        assert "\x01" not in draft.summary
        assert "\x7f" not in draft.investor_take

    def test_normalizes_nfkc(self) -> None:
        # NFKC は半角→全角の互換分解を畳む。
        draft = InScopeAssessmentDraft(
            translated_title="Hello",  # 全角英字を含む
            summary="ok",
            topic_name=TopicName(root="ai"),
            investor_take="reason",
        )
        assert draft.translated_title == "Hello"

    def test_preserves_newlines_and_tabs(self) -> None:
        draft = InScopeAssessmentDraft(
            translated_title="title",
            summary="line1\nline2",
            topic_name=TopicName(root="ai"),
            investor_take="reason\twith\ttabs",
        )
        assert "\n" in draft.summary
        assert "\t" in draft.investor_take


class TestInScopeAssessmentDraftRejection:
    def test_rejects_empty_translated_title(self) -> None:
        with pytest.raises(ValidationError):
            InScopeAssessmentDraft(
                translated_title="",
                summary="ok",
                topic_name=TopicName(root="ai"),
                investor_take="reason",
            )

    def test_rejects_title_that_becomes_empty_after_sanitization(self) -> None:
        with pytest.raises(ValidationError):
            InScopeAssessmentDraft(
                translated_title="<b></b>",
                summary="ok",
                topic_name=TopicName(root="ai"),
                investor_take="reason",
            )

    def test_rejects_translated_title_over_500_chars(self) -> None:
        with pytest.raises(ValidationError):
            InScopeAssessmentDraft(
                translated_title="a" * 501,
                summary="ok",
                topic_name=TopicName(root="ai"),
                investor_take="reason",
            )

    def test_rejects_summary_over_4000_chars(self) -> None:
        with pytest.raises(ValidationError):
            InScopeAssessmentDraft(
                translated_title="title",
                summary="a" * 4001,
                topic_name=TopicName(root="ai"),
                investor_take="reason",
            )

    def test_rejects_investor_take_over_2000_chars(self) -> None:
        with pytest.raises(ValidationError):
            InScopeAssessmentDraft(
                translated_title="title",
                summary="ok",
                topic_name=TopicName(root="ai"),
                investor_take="a" * 2001,
            )


class TestInScopeAssessmentDraftFromInScope:
    def test_builds_draft_with_sanitized_values(self) -> None:
        in_scope = _make_in_scope(investor_take="<b>bold</b>reason")
        draft = InScopeAssessmentDraft.from_in_scope(
            in_scope,
            translated_title="<i>title</i>",
            summary="summary",
        )
        assert draft.translated_title == "title"
        assert draft.summary == "summary"
        assert draft.investor_take == "boldreason"
        assert draft.topic_name == in_scope.topic

    def test_draft_is_frozen(self) -> None:
        draft = InScopeAssessmentDraft.from_in_scope(
            _make_in_scope(),
            translated_title="title",
            summary="summary",
        )
        with pytest.raises(ValidationError):
            draft.translated_title = "mutated"  # type: ignore[misc]


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


# Entity.from_draft は Pattern A' リファクタで廃止
# (Repository.save が直接 Entity を返すため Service 内での組み立て不要、
# spec §8 / typed-pipeline-preconditions.md)。


# ---------------------------------------------------------------------------
# OutOfScopeAssessmentDraft / OutOfScopeAssessment
# ---------------------------------------------------------------------------


class TestOutOfScopeAssessmentDraft:
    def test_strips_html_and_normalizes(self) -> None:
        draft = OutOfScopeAssessmentDraft(investor_take="<b>off-topic</b>\x00 article")
        assert "<b>" not in draft.investor_take
        assert "\x00" not in draft.investor_take

    def test_rejects_empty_investor_take(self) -> None:
        with pytest.raises(ValidationError):
            OutOfScopeAssessmentDraft(investor_take="")

    def test_rejects_investor_take_that_becomes_empty(self) -> None:
        with pytest.raises(ValidationError):
            OutOfScopeAssessmentDraft(investor_take="<i></i>")

    def test_rejects_investor_take_over_2000_chars(self) -> None:
        with pytest.raises(ValidationError):
            OutOfScopeAssessmentDraft(investor_take="a" * 2001)

    def test_from_out_of_scope_sanitizes(self) -> None:
        out_of_scope = OutOfScope(investor_take="<b>not tech</b>")
        draft = OutOfScopeAssessmentDraft.from_out_of_scope(out_of_scope)
        assert draft.investor_take == "not tech"

    def test_is_frozen(self) -> None:
        draft = OutOfScopeAssessmentDraft(investor_take="reason")
        with pytest.raises(ValidationError):
            draft.investor_take = "mutated"  # type: ignore[misc]


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


# OutOfScopeAssessment.from_draft も廃止
# (上記 InScopeAssessment.from_draft と同じ理由)。
