"""Agent core の最小入出力 contract。

API / UI / graph runtime から独立した final result の型だけをここで保証する。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.shared.security.safe_url import SafeUrl

__all__ = [
    "AnswerExecutionSummary",
    "AnswerQuestionInput",
    "AnswerQuestionResult",
    "AnswerRetrievalSummary",
    "AnswerSource",
    "ExecutionRoute",
    "ExternalUrlSource",
    "InternalArticleSource",
    "QuestionAnsweringAgent",
    "RetrievalMode",
    "UnmetRequirement",
]

RetrievalMode = Literal["none", "internal", "external", "internal_and_external"]
ExecutionRoute = Literal[
    "direct",
    "internal",
    "external_search",
    "internal_and_external",
    "workers",
]
UnmetRequirement = Literal["internal_retrieval", "external_search"]


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


class AnswerExecutionSummary(BaseModel):
    """agent が実際に通った主要経路の summary。"""

    model_config = ConfigDict(frozen=True)

    route: ExecutionRoute
    used_internal_retrieval: bool
    used_external_search: bool

    @model_validator(mode="after")
    def _validate_route_consistency(self) -> Self:
        expected = {
            "direct": (False, False),
            "internal": (True, False),
            "external_search": (False, True),
            "internal_and_external": (True, True),
        }.get(self.route)
        if expected is None:
            return self
        if (self.used_internal_retrieval, self.used_external_search) != expected:
            raise ValueError(f"{self.route} route has inconsistent retrieval flags")
        return self


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
    answer: str = Field(min_length=1)
    sources: list[AnswerSource] = Field(default_factory=list)
    missing_aspects: list[str] = Field(default_factory=list)
    retrieval: AnswerRetrievalSummary
    execution: AnswerExecutionSummary

    @model_validator(mode="after")
    def _validate_provenance(self) -> Self:
        if self.status == "answered":
            if self.execution.route != "direct" and not self.sources:
                raise ValueError("non-direct answered result must include a source")
            if self.missing_aspects:
                raise ValueError("answered result cannot include missing aspects")
            if self.retrieval.unmet_requirements:
                raise ValueError("answered result cannot include unmet requirements")
        has_external_source = any(
            isinstance(source, ExternalUrlSource) for source in self.sources
        )
        if (
            self.status == "answered"
            and self.execution.used_external_search
            and not has_external_source
        ):
            raise ValueError(
                "answered result using external search must include "
                "an external URL source"
            )
        return self


class QuestionAnsweringAgent(Protocol):
    """agent core の最小呼び出し口。"""

    async def answer(self, input: AnswerQuestionInput) -> AnswerQuestionResult: ...
