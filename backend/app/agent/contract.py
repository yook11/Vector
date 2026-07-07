"""Agent core の最小入出力 contract。

API / UI / graph runtime から独立した final result の型だけをここで保証する。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Protocol, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from app.shared.security.safe_url import SafeUrl

__all__ = [
    "AnswerQuestionInput",
    "AnswerQuestionResult",
    "AnswerRetrievalSummary",
    "AnswerSource",
    "ExternalUrlSource",
    "InternalArticleSource",
    "NonBlankText",
    "QuestionAnsweringAgent",
    "RetrievalMode",
    "UnmetRequirement",
]

RetrievalMode = Literal["none", "internal", "external", "internal_and_external"]
UnmetRequirement = Literal["internal_retrieval", "external_search"]
NonBlankText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]


class AnswerQuestionInput(BaseModel):
    """ユーザー質問と実行基準時刻を agent core に渡す入力。"""

    model_config = ConfigDict(frozen=True)

    question: str = Field(min_length=1)
    as_of: datetime


class AnswerRetrievalSummary(BaseModel):
    """planner が必要と判断した情報取得と、未充足の要件。"""

    model_config = ConfigDict(frozen=True)

    planned_mode: RetrievalMode
    unmet_requirements: list[UnmetRequirement] = Field(default_factory=list)


class InternalArticleSource(BaseModel):
    """内部分析済み記事に接地した回答 source。"""

    model_config = ConfigDict(frozen=True)

    kind: Literal["internal_article"] = "internal_article"
    source_ref: str = Field(min_length=1)
    article_id: int = Field(gt=0)
    title: str = Field(min_length=1)
    snippet: str | None = None
    published_at: datetime | None = None
    source_name: str | None = None


class ExternalUrlSource(BaseModel):
    """外部 URL に接地した回答 source。"""

    model_config = ConfigDict(frozen=True)

    kind: Literal["external_url"] = "external_url"
    source_ref: str = Field(min_length=1)
    url: SafeUrl
    title: str = Field(min_length=1)
    snippet: str | None = None
    published_at: datetime | None = None
    source_name: str | None = None


AnswerSource = Annotated[
    InternalArticleSource | ExternalUrlSource,
    Field(discriminator="kind"),
]


class AnswerQuestionResult(BaseModel):
    """chat UI に変換される agent core の final result。"""

    model_config = ConfigDict(frozen=True)

    status: Literal["answered", "insufficient"]
    answer: NonBlankText
    sources: list[AnswerSource] = Field(default_factory=list)
    missing_aspects: list[NonBlankText] = Field(default_factory=list)
    retrieval: AnswerRetrievalSummary

    @model_validator(mode="after")
    def _validate_provenance(self) -> Self:
        if self.status == "answered":
            if self.retrieval.planned_mode != "none" and not self.sources:
                raise ValueError("non-direct answered result must include a source")
            if self.missing_aspects:
                raise ValueError("answered result cannot include missing aspects")
            if self.retrieval.unmet_requirements:
                raise ValueError("answered result cannot include unmet requirements")
        if self.status == "insufficient" and not self.missing_aspects:
            raise ValueError("insufficient result must include missing aspects")
        if self.retrieval.planned_mode == "none" and self.sources:
            raise ValueError("direct planned result cannot include sources")
        return self


class QuestionAnsweringAgent(Protocol):
    """agent core の最小呼び出し口。"""

    async def answer(self, input: AnswerQuestionInput) -> AnswerQuestionResult: ...
