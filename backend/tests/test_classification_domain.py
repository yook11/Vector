"""classification ドメイン層のユニットテスト (DB 不要)。

Draft の sanitize / validation、Entity の __post_init__、
ファクトリ (from_classified / from_out_of_scope / from_draft) を検証する。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.analysis.classification.domain.analysis import Analysis, AnalysisDraft
from app.analysis.classification.domain.rejection import Rejection, RejectionDraft
from app.analysis.classifier.schema import Classified, OutOfScope, ValidCategory
from app.analysis.domain.value_objects.topic import TopicName


def _make_classified(**overrides: object) -> Classified:
    defaults: dict[str, object] = {
        "category": ValidCategory.AI,
        "topic": TopicName(root="ai agents"),
        "investor_take": "Significant advancement in agent autonomy.",
    }
    defaults.update(overrides)
    return Classified(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# AnalysisDraft — sanitize / validation
# ---------------------------------------------------------------------------


class TestAnalysisDraftSanitize:
    def test_strips_html_tags_from_title_summary_investor_take(self) -> None:
        draft = AnalysisDraft(
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
        draft = AnalysisDraft(
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
        draft = AnalysisDraft(
            translated_title="Hello",  # 全角英字を含む
            summary="ok",
            topic_name=TopicName(root="ai"),
            investor_take="reason",
        )
        assert draft.translated_title == "Hello"

    def test_preserves_newlines_and_tabs(self) -> None:
        draft = AnalysisDraft(
            translated_title="title",
            summary="line1\nline2",
            topic_name=TopicName(root="ai"),
            investor_take="reason\twith\ttabs",
        )
        assert "\n" in draft.summary
        assert "\t" in draft.investor_take


class TestAnalysisDraftRejection:
    def test_rejects_empty_translated_title(self) -> None:
        with pytest.raises(ValidationError):
            AnalysisDraft(
                translated_title="",
                summary="ok",
                topic_name=TopicName(root="ai"),
                investor_take="reason",
            )

    def test_rejects_title_that_becomes_empty_after_sanitization(self) -> None:
        with pytest.raises(ValidationError):
            AnalysisDraft(
                translated_title="<b></b>",
                summary="ok",
                topic_name=TopicName(root="ai"),
                investor_take="reason",
            )

    def test_rejects_translated_title_over_500_chars(self) -> None:
        with pytest.raises(ValidationError):
            AnalysisDraft(
                translated_title="a" * 501,
                summary="ok",
                topic_name=TopicName(root="ai"),
                investor_take="reason",
            )

    def test_rejects_summary_over_4000_chars(self) -> None:
        with pytest.raises(ValidationError):
            AnalysisDraft(
                translated_title="title",
                summary="a" * 4001,
                topic_name=TopicName(root="ai"),
                investor_take="reason",
            )

    def test_rejects_investor_take_over_2000_chars(self) -> None:
        with pytest.raises(ValidationError):
            AnalysisDraft(
                translated_title="title",
                summary="ok",
                topic_name=TopicName(root="ai"),
                investor_take="a" * 2001,
            )


class TestAnalysisDraftFromClassified:
    def test_builds_draft_with_sanitized_values(self) -> None:
        classified = _make_classified(investor_take="<b>bold</b>reason")
        draft = AnalysisDraft.from_classified(
            classified,
            translated_title="<i>title</i>",
            summary="summary",
        )
        assert draft.translated_title == "title"
        assert draft.summary == "summary"
        assert draft.investor_take == "boldreason"
        assert draft.topic_name == classified.topic

    def test_draft_is_frozen(self) -> None:
        draft = AnalysisDraft.from_classified(
            _make_classified(),
            translated_title="title",
            summary="summary",
        )
        with pytest.raises(ValidationError):
            draft.translated_title = "mutated"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Analysis Entity — __post_init__ / from_draft
# ---------------------------------------------------------------------------


def _make_analysis(**overrides: object) -> Analysis:
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
    return Analysis(**defaults)  # type: ignore[arg-type]


class TestAnalysisPostInit:
    def test_constructs_with_valid_args(self) -> None:
        analysis = _make_analysis()
        assert analysis.id == 1

    @pytest.mark.parametrize(
        "field",
        ["translated_title", "summary", "investor_take", "ai_model"],
    )
    def test_rejects_empty_string_fields(self, field: str) -> None:
        with pytest.raises(ValueError):
            _make_analysis(**{field: ""})

    @pytest.mark.parametrize("field", ["id", "extraction_id", "category_id"])
    @pytest.mark.parametrize("value", [0, -1])
    def test_rejects_non_positive_identifiers(self, field: str, value: int) -> None:
        with pytest.raises(ValueError):
            _make_analysis(**{field: value})

    def test_is_frozen(self) -> None:
        analysis = _make_analysis()
        with pytest.raises((AttributeError, TypeError)):
            analysis.id = 999  # type: ignore[misc]


class TestAnalysisFromDraft:
    def test_combines_draft_with_identity(self) -> None:
        draft = AnalysisDraft.from_classified(
            _make_classified(),
            translated_title="title",
            summary="summary",
        )
        analyzed_at = datetime(2026, 4, 25, tzinfo=UTC)
        analysis = Analysis.from_draft(
            draft,
            id=42,
            extraction_id=7,
            category_id=3,
            ai_model="gemini-2.5-pro",
            analyzed_at=analyzed_at,
        )
        assert analysis.id == 42
        assert analysis.extraction_id == 7
        assert analysis.category_id == 3
        assert analysis.topic == draft.topic_name
        assert analysis.ai_model == "gemini-2.5-pro"
        assert analysis.analyzed_at == analyzed_at
        assert analysis.translated_title == draft.translated_title
        assert analysis.investor_take == draft.investor_take


# ---------------------------------------------------------------------------
# RejectionDraft / Rejection
# ---------------------------------------------------------------------------


class TestRejectionDraft:
    def test_strips_html_and_normalizes(self) -> None:
        draft = RejectionDraft(investor_take="<b>off-topic</b>\x00 article")
        assert "<b>" not in draft.investor_take
        assert "\x00" not in draft.investor_take

    def test_rejects_empty_investor_take(self) -> None:
        with pytest.raises(ValidationError):
            RejectionDraft(investor_take="")

    def test_rejects_investor_take_that_becomes_empty(self) -> None:
        with pytest.raises(ValidationError):
            RejectionDraft(investor_take="<i></i>")

    def test_rejects_investor_take_over_2000_chars(self) -> None:
        with pytest.raises(ValidationError):
            RejectionDraft(investor_take="a" * 2001)

    def test_from_out_of_scope_sanitizes(self) -> None:
        out_of_scope = OutOfScope(investor_take="<b>not tech</b>")
        draft = RejectionDraft.from_out_of_scope(out_of_scope)
        assert draft.investor_take == "not tech"

    def test_is_frozen(self) -> None:
        draft = RejectionDraft(investor_take="reason")
        with pytest.raises(ValidationError):
            draft.investor_take = "mutated"  # type: ignore[misc]


def _make_rejection(**overrides: object) -> Rejection:
    defaults: dict[str, object] = {
        "id": 1,
        "extraction_id": 2,
        "investor_take": "out of scope",
        "ai_model": "gemini-2.5-pro",
        "rejected_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    defaults.update(overrides)
    return Rejection(**defaults)  # type: ignore[arg-type]


class TestRejectionPostInit:
    def test_constructs_with_valid_args(self) -> None:
        rejection = _make_rejection()
        assert rejection.id == 1

    @pytest.mark.parametrize("field", ["investor_take", "ai_model"])
    def test_rejects_empty_string_fields(self, field: str) -> None:
        with pytest.raises(ValueError):
            _make_rejection(**{field: ""})

    @pytest.mark.parametrize("field", ["id", "extraction_id"])
    @pytest.mark.parametrize("value", [0, -1])
    def test_rejects_non_positive_identifiers(self, field: str, value: int) -> None:
        with pytest.raises(ValueError):
            _make_rejection(**{field: value})

    def test_is_frozen(self) -> None:
        rejection = _make_rejection()
        with pytest.raises((AttributeError, TypeError)):
            rejection.id = 999  # type: ignore[misc]


class TestRejectionFromDraft:
    def test_combines_draft_with_identity(self) -> None:
        draft = RejectionDraft.from_out_of_scope(OutOfScope(investor_take="not tech"))
        rejected_at = datetime(2026, 4, 25, tzinfo=UTC)
        rejection = Rejection.from_draft(
            draft,
            id=99,
            extraction_id=7,
            ai_model="gemini-2.5-pro",
            rejected_at=rejected_at,
        )
        assert rejection.id == 99
        assert rejection.extraction_id == 7
        assert rejection.ai_model == "gemini-2.5-pro"
        assert rejection.rejected_at == rejected_at
        assert rejection.investor_take == draft.investor_take
