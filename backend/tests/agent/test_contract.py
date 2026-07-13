"""Agent core contract の unit tests。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.agent.contract import (
    AnswerQuestionInput,
    AnswerQuestionResult,
    AnswerRetrievalSummary,
    EvidenceCollectionFailure,
    ExternalUrlSource,
    InternalArticleSource,
    RetrievalMode,
)
from app.agent.question_context.contract import QuestionContext


def _as_of() -> datetime:
    return datetime(2026, 6, 27, tzinfo=UTC)


def _internal_source() -> InternalArticleSource:
    return InternalArticleSource(
        source_ref="source_1",
        article_id=1,
        title="内部記事",
    )


def _external_source() -> ExternalUrlSource:
    return ExternalUrlSource(
        source_ref="source_2",
        url="https://example.com/news",
        title="外部記事",
        evidence_claim="外部記事が支える主張",
    )


def _retrieval(
    planned_mode: RetrievalMode = "internal",
    collection_failures: list[EvidenceCollectionFailure] | None = None,
) -> AnswerRetrievalSummary:
    return AnswerRetrievalSummary(
        planned_mode=planned_mode,
        collection_failures=collection_failures or [],
    )


class TestAnswerQuestionInput:
    def test_holds_only_shared_context_execution_time_and_previous_answer(self) -> None:
        context = QuestionContext(standalone_question="NVIDIA の直近動向は？")
        input_ = AnswerQuestionInput(
            context=context,
            as_of=_as_of(),
            previous_answer="前回の回答本文",
        )

        with pytest.raises(ValidationError):
            input_.previous_answer = "変更不可"

        assert (
            set(AnswerQuestionInput.model_fields),
            input_.context is context,
            input_.as_of,
            input_.previous_answer,
        ) == (
            {"context", "as_of", "previous_answer"},
            True,
            _as_of(),
            "前回の回答本文",
        )

    def test_rejects_legacy_flat_context_fields(self) -> None:
        with pytest.raises(ValidationError):
            AnswerQuestionInput(
                context=QuestionContext(standalone_question="NVIDIA の直近動向は？"),
                as_of=_as_of(),
                question="legacy question",
            )


class TestAnswerRetrievalSummary:
    @pytest.mark.parametrize("failure", ["internal_search", "external_search"])
    def test_accepts_planned_mode_and_collection_failures(
        self,
        failure: EvidenceCollectionFailure,
    ) -> None:
        summary = AnswerRetrievalSummary(
            planned_mode="internal_and_external",
            collection_failures=[failure],
        )

        assert summary.planned_mode == "internal_and_external"
        assert summary.collection_failures == [failure]


class TestSources:
    def test_rejects_non_positive_internal_article_id(self) -> None:
        with pytest.raises(ValidationError):
            InternalArticleSource(
                source_ref="source_1",
                article_id=0,
                title="内部記事",
            )

    def test_rejects_invalid_external_url(self) -> None:
        with pytest.raises(ValidationError):
            ExternalUrlSource(
                source_ref="source_1",
                url="file:///tmp/news",
                title="外部記事",
                evidence_claim="外部記事が支える主張",
            )

    def test_rejects_blank_external_evidence_claim(self) -> None:
        with pytest.raises(ValidationError):
            ExternalUrlSource(
                source_ref="source_1",
                url="https://example.com/news",
                title="外部記事",
                evidence_claim="   ",
            )


class TestAnswerQuestionResult:
    def test_accepts_direct_answered_result_without_source(self) -> None:
        result = AnswerQuestionResult(
            status="answered",
            answer="こんにちは。何を確認しますか？",
            retrieval=_retrieval("none"),
        )

        assert result.sources == []
        assert not hasattr(result, "execution")

    def test_accepts_internal_answered_result_with_source(self) -> None:
        result = AnswerQuestionResult(
            status="answered",
            answer="内部記事から確認できました。",
            sources=[_internal_source()],
            retrieval=_retrieval("internal"),
        )

        assert result.status == "answered"

    def test_accepts_insufficient_without_sources_when_missing_is_present(self) -> None:
        result = AnswerQuestionResult(
            status="insufficient",
            answer="確認できた範囲では断定できません。",
            missing_aspects=["企業側の一次情報"],
            retrieval=_retrieval("internal"),
        )

        assert result.sources == []

    def test_rejects_non_direct_answered_result_without_sources(self) -> None:
        with pytest.raises(ValidationError):
            AnswerQuestionResult(
                status="answered",
                answer="確認できました。",
                retrieval=_retrieval("internal"),
            )

    def test_rejects_answered_result_with_missing_aspects(self) -> None:
        with pytest.raises(ValidationError):
            AnswerQuestionResult(
                status="answered",
                answer="確認できました。",
                sources=[_internal_source()],
                missing_aspects=["企業側の一次情報"],
                retrieval=_retrieval("internal"),
            )

    def test_rejects_answered_result_with_collection_failures(self) -> None:
        with pytest.raises(ValidationError):
            AnswerQuestionResult(
                status="answered",
                answer="確認できました。",
                sources=[_external_source()],
                retrieval=_retrieval("external", ["external_search"]),
            )

    def test_rejects_direct_planned_mode_with_sources(self) -> None:
        with pytest.raises(ValidationError):
            AnswerQuestionResult(
                status="answered",
                answer="検索なし回答です。",
                sources=[_internal_source()],
                retrieval=_retrieval("none"),
            )

    def test_rejects_insufficient_without_missing_aspects(self) -> None:
        with pytest.raises(ValidationError):
            AnswerQuestionResult(
                status="insufficient",
                answer="確認できた範囲では断定できません。",
                retrieval=_retrieval("internal"),
            )

    @pytest.mark.parametrize("answer", ["", "   ", "\n"])
    def test_rejects_blank_answer(self, answer: str) -> None:
        with pytest.raises(ValidationError):
            AnswerQuestionResult(
                status="insufficient",
                answer=answer,
                missing_aspects=["企業側の一次情報"],
                retrieval=_retrieval("internal"),
            )

    @pytest.mark.parametrize("missing", ["", "   ", "\n"])
    def test_rejects_blank_missing_aspect(self, missing: str) -> None:
        with pytest.raises(ValidationError):
            AnswerQuestionResult(
                status="insufficient",
                answer="確認できた範囲では断定できません。",
                missing_aspects=[missing],
                retrieval=_retrieval("internal"),
            )
