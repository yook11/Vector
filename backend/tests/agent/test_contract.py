"""Agent core contract の unit tests。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.agent.contract import (
    AnswerQuestionInput,
    AnswerQuestionResult,
    AnswerRetrievalSummary,
    ExternalUrlSource,
    InternalArticleSource,
    RetrievalMode,
    UnmetRequirement,
)


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
    )


def _retrieval(
    planned_mode: RetrievalMode = "internal",
    unmet_requirements: list[UnmetRequirement] | None = None,
) -> AnswerRetrievalSummary:
    return AnswerRetrievalSummary(
        planned_mode=planned_mode,
        unmet_requirements=unmet_requirements or [],
    )


class TestAnswerQuestionInput:
    def test_accepts_question_and_as_of(self) -> None:
        input_ = AnswerQuestionInput(question="NVIDIA の直近動向は？", as_of=_as_of())

        assert input_.question == "NVIDIA の直近動向は？"
        assert input_.as_of == _as_of()

    def test_rejects_empty_question(self) -> None:
        with pytest.raises(ValidationError):
            AnswerQuestionInput(question="", as_of=_as_of())


class TestAnswerRetrievalSummary:
    def test_accepts_planned_mode_and_unmet_requirements(self) -> None:
        summary = AnswerRetrievalSummary(
            planned_mode="external",
            unmet_requirements=["external_search"],
        )

        assert summary.planned_mode == "external"
        assert summary.unmet_requirements == ["external_search"]


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

    def test_rejects_answered_result_with_unmet_requirements(self) -> None:
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
