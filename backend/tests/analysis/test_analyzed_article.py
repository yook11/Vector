"""In-scope analyzed article snapshot の domain 契約テスト。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.analysis.analyzed_article import InScopeAnalyzedArticle
from app.analysis.assessment.domain.ready import ReadyForAssessment
from app.analysis.assessment.domain.result import (
    InScope,
    InScopeCategory,
    KeyPoint,
    Mention,
    MentionType,
)


def _ready() -> ReadyForAssessment:
    return ReadyForAssessment(
        curation_id=123,
        translated_title="翻訳タイトル",
        summary="要約本文",
        analyzable_article_id=456,
    )


def _in_scope(
    *,
    category: InScopeCategory = InScopeCategory.AI,
    investor_take: str = "投資家向け見解",
    key_points: list[KeyPoint] | None = None,
) -> InScope:
    return InScope(
        category=category,
        investor_take=investor_take,
        key_points=key_points or [],
    )


def test_from_ready_and_assessment_result_builds_snapshot() -> None:
    ready = _ready()
    assessment_result = _in_scope(
        key_points=[
            KeyPoint(
                content="OpenAI が新モデルを発表した。",
                mentions=[Mention(surface="OpenAI", type=MentionType.COMPANY)],
            )
        ]
    )

    article = InScopeAnalyzedArticle.from_ready_and_assessment_result(
        ready=ready,
        assessment_result=assessment_result,
    )

    assert article.curation_id == ready.curation_id
    assert article.title == ready.translated_title
    assert article.summary == ready.summary
    assert article.assessment_result == assessment_result
    assert article.assessment_result.category is InScopeCategory.AI


@pytest.mark.parametrize(
    "values",
    [
        {"curation_id": 0, "title": "t", "summary": "s"},
        {"curation_id": -1, "title": "t", "summary": "s"},
        {"curation_id": 1, "title": "", "summary": "s"},
        {"curation_id": 1, "title": "t", "summary": ""},
    ],
)
def test_rejects_invalid_snapshot_fields(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        InScopeAnalyzedArticle(
            **values,
            assessment_result=_in_scope(),
        )


def test_rejects_out_of_scope_category_in_assessment_result() -> None:
    with pytest.raises(ValidationError):
        InScopeAnalyzedArticle.model_validate(
            {
                "curation_id": 1,
                "title": "t",
                "summary": "s",
                "assessment_result": {
                    "category": "out_of_scope",
                    "investor_take": "x",
                },
            }
        )


@pytest.mark.parametrize("field_name", ["title", "summary", "assessment_result"])
def test_snapshot_is_frozen(field_name: str) -> None:
    article = InScopeAnalyzedArticle.from_ready_and_assessment_result(
        ready=_ready(),
        assessment_result=_in_scope(),
    )

    with pytest.raises(ValidationError):
        setattr(article, field_name, "changed")


def test_from_persisted_values_reconstructs_snapshot() -> None:
    article = InScopeAnalyzedArticle.from_persisted_values(
        curation_id=321,
        translated_title="保存済みタイトル",
        summary="保存済み要約",
        category_slug="ai",
        investor_take="保存済み見解",
        key_points=[
            {
                "content": "Anthropic が新機能を公開した。",
                "mentions": [
                    {"surface": "Anthropic", "type": "company"},
                    {"surface": "Claude", "type": "product"},
                ],
            }
        ],
    )

    assert article.curation_id == 321
    assert article.title == "保存済みタイトル"
    assert article.summary == "保存済み要約"
    assert article.assessment_result.category is InScopeCategory.AI
    assert article.assessment_result.investor_take == "保存済み見解"
    assert article.assessment_result.key_points == [
        KeyPoint(
            content="Anthropic が新機能を公開した。",
            mentions=[
                Mention(surface="Anthropic", type=MentionType.COMPANY),
                Mention(surface="Claude", type=MentionType.PRODUCT),
            ],
        )
    ]


def test_from_persisted_values_normalizes_null_key_points_to_empty_list() -> None:
    article = InScopeAnalyzedArticle.from_persisted_values(
        curation_id=321,
        translated_title="保存済みタイトル",
        summary="保存済み要約",
        category_slug="ai",
        investor_take="保存済み見解",
        key_points=None,
    )

    assert article.assessment_result.key_points == []


@pytest.mark.parametrize(
    ("category_slug", "key_points"),
    [
        ("out_of_scope", []),
        ("unknown", []),
        ("ai", "not-a-list"),
        ("ai", [{"content": "", "mentions": []}]),
    ],
)
def test_from_persisted_values_rejects_invalid_persisted_values(
    category_slug: str,
    key_points: object,
) -> None:
    with pytest.raises(ValidationError):
        InScopeAnalyzedArticle.from_persisted_values(
            curation_id=321,
            translated_title="保存済みタイトル",
            summary="保存済み要約",
            category_slug=category_slug,
            investor_take="保存済み見解",
            key_points=key_points,
        )
