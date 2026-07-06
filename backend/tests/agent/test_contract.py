"""Agent core contract の unit tests。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.agent.contract import (
    AnswerExecutionSummary,
    AnswerQuestionInput,
    AnswerQuestionResult,
    AnswerRetrievalSummary,
    ExecutionRoute,
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


def _execution(
    route: ExecutionRoute = "internal",
    *,
    used_internal_retrieval: bool = True,
    used_external_search: bool = False,
) -> AnswerExecutionSummary:
    return AnswerExecutionSummary(
        route=route,
        used_internal_retrieval=used_internal_retrieval,
        used_external_search=used_external_search,
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


class TestAnswerExecutionSummary:
    def test_accepts_direct_without_any_retrieval(self) -> None:
        summary = _execution(
            "direct",
            used_internal_retrieval=False,
            used_external_search=False,
        )

        assert summary.route == "direct"

    def test_rejects_direct_with_internal_retrieval(self) -> None:
        with pytest.raises(ValidationError):
            _execution(
                "direct",
                used_internal_retrieval=True,
                used_external_search=False,
            )

    def test_accepts_internal_with_internal_retrieval_only(self) -> None:
        summary = _execution(
            "internal",
            used_internal_retrieval=True,
            used_external_search=False,
        )

        assert summary.used_internal_retrieval is True

    def test_rejects_internal_with_external_search(self) -> None:
        with pytest.raises(ValidationError):
            _execution(
                "internal",
                used_internal_retrieval=True,
                used_external_search=True,
            )

    def test_accepts_external_search_with_external_search_only(self) -> None:
        summary = _execution(
            "external_search",
            used_internal_retrieval=False,
            used_external_search=True,
        )

        assert summary.used_external_search is True

    def test_rejects_external_search_with_internal_retrieval(self) -> None:
        with pytest.raises(ValidationError):
            _execution(
                "external_search",
                used_internal_retrieval=True,
                used_external_search=True,
            )

    def test_accepts_internal_and_external_with_both_retrievals(self) -> None:
        summary = _execution(
            "internal_and_external",
            used_internal_retrieval=True,
            used_external_search=True,
        )

        assert summary.route == "internal_and_external"

    @pytest.mark.parametrize("used_internal_retrieval", [False, True])
    @pytest.mark.parametrize("used_external_search", [False, True])
    def test_workers_allows_retrieval_flags_either_way(
        self,
        used_internal_retrieval: bool,
        used_external_search: bool,
    ) -> None:
        summary = _execution(
            "workers",
            used_internal_retrieval=used_internal_retrieval,
            used_external_search=used_external_search,
        )

        assert summary.route == "workers"


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
            execution=_execution(
                "direct",
                used_internal_retrieval=False,
                used_external_search=False,
            ),
        )

        assert result.sources == []

    def test_accepts_internal_answered_result_with_source(self) -> None:
        result = AnswerQuestionResult(
            status="answered",
            answer="内部記事から確認できました。",
            sources=[_internal_source()],
            retrieval=_retrieval("internal"),
            execution=_execution(
                "internal",
                used_internal_retrieval=True,
                used_external_search=False,
            ),
        )

        assert result.status == "answered"

    def test_rejects_non_direct_answered_result_without_sources(self) -> None:
        with pytest.raises(ValidationError):
            AnswerQuestionResult(
                status="answered",
                answer="確認できました。",
                retrieval=_retrieval("internal"),
                execution=_execution(
                    "internal",
                    used_internal_retrieval=True,
                    used_external_search=False,
                ),
            )

    def test_rejects_answered_result_with_missing_aspects(self) -> None:
        with pytest.raises(ValidationError):
            AnswerQuestionResult(
                status="answered",
                answer="確認できました。",
                sources=[_internal_source()],
                missing_aspects=["企業側の一次情報"],
                retrieval=_retrieval("internal"),
                execution=_execution(
                    "internal",
                    used_internal_retrieval=True,
                    used_external_search=False,
                ),
            )

    def test_rejects_answered_result_with_unmet_requirements(self) -> None:
        with pytest.raises(ValidationError):
            AnswerQuestionResult(
                status="answered",
                answer="確認できました。",
                retrieval=_retrieval("external", ["external_search"]),
                execution=_execution(
                    "direct",
                    used_internal_retrieval=False,
                    used_external_search=False,
                ),
            )

    def test_rejects_answered_external_search_result_without_external_source(
        self,
    ) -> None:
        with pytest.raises(ValidationError):
            AnswerQuestionResult(
                status="answered",
                answer="外部検索も確認しました。",
                sources=[_internal_source()],
                retrieval=_retrieval("external"),
                execution=_execution(
                    "external_search",
                    used_internal_retrieval=False,
                    used_external_search=True,
                ),
            )

    def test_accepts_external_search_result_with_external_source(self) -> None:
        result = AnswerQuestionResult(
            status="answered",
            answer="外部ニュースも確認しました。",
            sources=[_internal_source(), _external_source()],
            retrieval=_retrieval("external"),
            execution=_execution(
                "external_search",
                used_internal_retrieval=False,
                used_external_search=True,
            ),
        )

        assert any(isinstance(source, ExternalUrlSource) for source in result.sources)

    def test_accepts_external_unavailable_insufficient_without_sources(self) -> None:
        result = AnswerQuestionResult(
            status="insufficient",
            answer="この質問には外部最新情報の確認が必要です。",
            missing_aspects=["外部ニュース検索"],
            retrieval=_retrieval("external", ["external_search"]),
            execution=_execution(
                "direct",
                used_internal_retrieval=False,
                used_external_search=False,
            ),
        )

        assert result.sources == []
        assert result.retrieval.unmet_requirements == ["external_search"]
        assert result.execution.route == "direct"

    def test_accepts_insufficient_without_sources(self) -> None:
        result = AnswerQuestionResult(
            status="insufficient",
            answer="確認できた範囲では断定できません。",
            missing_aspects=["企業側の一次情報"],
            retrieval=_retrieval("internal"),
            execution=_execution(
                "internal",
                used_internal_retrieval=True,
                used_external_search=False,
            ),
        )

        assert result.sources == []

    def test_rejects_empty_answer_even_when_insufficient(self) -> None:
        with pytest.raises(ValidationError):
            AnswerQuestionResult(
                status="insufficient",
                answer="",
                retrieval=_retrieval("internal"),
                execution=_execution(
                    "internal",
                    used_internal_retrieval=True,
                    used_external_search=False,
                ),
            )
