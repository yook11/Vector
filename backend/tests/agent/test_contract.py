"""Agent core contract の unit tests。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import app.agent as agent_package
import app.agent.answering as answering_package
import app.agent.composition as composition
import app.agent.contract as agent_contract
from app.agent.contract import (
    AnswerQuestionResult,
    EvidenceCollectionFailure,
    ExternalUrlSource,
    InternalArticleSource,
)


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


def _plan_summary(
    plan_type: str = "search",
    collection_failures: list[EvidenceCollectionFailure] | None = None,
) -> object:
    summary_type = getattr(agent_contract, "AnswerPlanSummary", None)
    if summary_type is None:
        pytest.fail("agent contract must define AnswerPlanSummary")
    return summary_type(
        plan_type=plan_type,
        collection_failures=collection_failures or [],
    )


def test_does_not_export_legacy_answering_boundaries() -> None:
    assert (
        hasattr(agent_contract, "AnswerQuestionInput"),
        hasattr(agent_contract, "QuestionAnsweringAgent"),
        hasattr(answering_package, "QuestionAnsweringOrchestrator"),
        hasattr(composition, "build_question_answering_starting_agent"),
        hasattr(composition, "build_question_answering_agent"),
        hasattr(agent_package, "AnswerQuestionInput"),
        hasattr(agent_package, "QuestionAnsweringAgent"),
    ) == (False, False, False, False, False, False, False)


class TestAnswerPlanSummary:
    @pytest.mark.parametrize("failure", ["internal_search", "external_search"])
    def test_accepts_search_plan_type_and_collection_failures(
        self,
        failure: EvidenceCollectionFailure,
    ) -> None:
        summary = _plan_summary(
            plan_type="search",
            collection_failures=[failure],
        )

        assert summary.plan_type == "search"
        assert summary.collection_failures == [failure]

    def test_rejects_collection_failures_for_direct_answer(self) -> None:
        with pytest.raises(ValidationError):
            _plan_summary(
                plan_type="direct_answer",
                collection_failures=["internal_search"],
            )


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
            plan_summary=_plan_summary("direct_answer"),
        )

        assert result.sources == []
        assert not hasattr(result, "execution")

    def test_accepts_search_answered_result_with_internal_source(self) -> None:
        result = AnswerQuestionResult(
            status="answered",
            answer="内部記事から確認できました。",
            sources=[_internal_source()],
            plan_summary=_plan_summary("search"),
        )

        assert result.status == "answered"

    def test_accepts_insufficient_without_sources_when_missing_is_present(self) -> None:
        result = AnswerQuestionResult(
            status="insufficient",
            answer="確認できた範囲では断定できません。",
            missing_aspects=["企業側の一次情報"],
            plan_summary=_plan_summary("search"),
        )

        assert result.sources == []

    def test_rejects_search_answered_result_without_sources(self) -> None:
        with pytest.raises(ValidationError):
            AnswerQuestionResult(
                status="answered",
                answer="確認できました。",
                plan_summary=_plan_summary("search"),
            )

    def test_rejects_answered_result_with_missing_aspects(self) -> None:
        with pytest.raises(ValidationError):
            AnswerQuestionResult(
                status="answered",
                answer="確認できました。",
                sources=[_internal_source()],
                missing_aspects=["企業側の一次情報"],
                plan_summary=_plan_summary("search"),
            )

    def test_rejects_answered_result_with_collection_failures(self) -> None:
        with pytest.raises(ValidationError):
            AnswerQuestionResult(
                status="answered",
                answer="確認できました。",
                sources=[_external_source()],
                plan_summary=_plan_summary("search", ["external_search"]),
            )

    def test_rejects_direct_plan_type_with_sources(self) -> None:
        with pytest.raises(ValidationError):
            AnswerQuestionResult(
                status="answered",
                answer="検索なし回答です。",
                sources=[_internal_source()],
                plan_summary=_plan_summary("direct_answer"),
            )

    def test_rejects_insufficient_without_missing_aspects(self) -> None:
        with pytest.raises(ValidationError):
            AnswerQuestionResult(
                status="insufficient",
                answer="確認できた範囲では断定できません。",
                plan_summary=_plan_summary("search"),
            )

    @pytest.mark.parametrize("answer", ["", "   ", "\n"])
    def test_rejects_blank_answer(self, answer: str) -> None:
        with pytest.raises(ValidationError):
            AnswerQuestionResult(
                status="insufficient",
                answer=answer,
                missing_aspects=["企業側の一次情報"],
                plan_summary=_plan_summary("search"),
            )

    @pytest.mark.parametrize("missing", ["", "   ", "\n"])
    def test_rejects_blank_missing_aspect(self, missing: str) -> None:
        with pytest.raises(ValidationError):
            AnswerQuestionResult(
                status="insufficient",
                answer="確認できた範囲では断定できません。",
                missing_aspects=[missing],
                plan_summary=_plan_summary("search"),
            )
