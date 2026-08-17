"""External search の境界型・port 契約・構造 cap 定数。

Agent / workflow / provider adapter が共有する frozen model と Protocol を
ここで保証する。
自由記述欄の clamp は from_raw factory で行い、model validator は
「factory を通れば違反しない」不変条件として保持する。
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.agent.contract import (
    EVIDENCE_CLAIM_MAX_CHARS,
    EXTERNAL_QUERY_MAX_CHARS,
    EXTERNAL_TASK_QUERY_LIMIT,
    MISSING_ITEM_MAX_CHARS,
)
from app.agent.planning.contract import ExternalResearchTask, TargetTimeWindow
from app.agent.runtime.contract import AgentRuntime
from app.shared.security.safe_url import SafeUrl

__all__ = [
    "EVIDENCE_CLAIM_MAX_CHARS",
    "EVIDENCE_WHY_SELECTED_MAX_CHARS",
    "EXTERNAL_CONTENT_MAX_CHARS",
    "EXTERNAL_QUERY_MAX_CHARS",
    "EXTERNAL_SEARCH_AGENT_HARD_LIMIT",
    "EXTERNAL_SEARCH_HITS_PER_QUERY",
    "EXTERNAL_SEARCH_HIT_POOL_LIMIT_PER_TASK",
    "EXTERNAL_SEARCH_TOOL_NAME",
    "EXTERNAL_TASK_QUERY_LIMIT",
    "ExternalQueryDraft",
    "ExternalQueryGenerationInput",
    "ExternalSearchHit",
    "ExternalSearchDateFilter",
    "ExternalSearchProviderError",
    "ExternalResearchRuntime",
    "ExternalResearchRuntimeFactory",
    "ExternalSearchTool",
    "ExternalSearchToolFailureReason",
    "ExternalSearchToolInput",
    "ExternalSearchToolName",
    "MISSING_ITEM_MAX_CHARS",
    "TimeFilterFailureReason",
]

EXTERNAL_SEARCH_AGENT_HARD_LIMIT = 3
EXTERNAL_SEARCH_HITS_PER_QUERY = 10
EXTERNAL_SEARCH_HIT_POOL_LIMIT_PER_TASK = 20
EVIDENCE_WHY_SELECTED_MAX_CHARS = 300
# 収集した外部記事の異常値を丸めるための上限。Reviewer表示の予算とは別。
EXTERNAL_CONTENT_MAX_CHARS = 1000

TimeFilterFailureReason = Literal[
    "future_calendar_month",
    "future_date_range",
    "unexpandable_start_date",
    "unsupported_explicit_window",
]


ExternalSearchToolName = Literal["external_search"]
EXTERNAL_SEARCH_TOOL_NAME: Final[ExternalSearchToolName] = "external_search"


class ExternalSearchToolFailureReason(StrEnum):
    """External Search Toolが公開できるprovider failureの分類。"""

    HTTP_ERROR = "tavily_search_http_error"
    HTTP_STATUS = "tavily_search_http_status"
    INVALID_JSON = "tavily_search_invalid_json"
    INVALID_RESULTS = "tavily_search_invalid_results"
    # egress proxy 段で失敗した (provider 障害ではない)。AWS では allowlist の設定ミスが
    # ここに来る。実際の status と拒否された宛先は proxy の access log 側にある。
    PROXY_ERROR = "tavily_search_proxy_error"


class ExternalSearchProviderError(Exception):
    """External Search Toolが安全なreasonだけを公開する分類済みerror。"""

    __slots__ = ("reason",)

    def __init__(
        self,
        *,
        reason: ExternalSearchToolFailureReason | str,
        status_code: int | None = None,
    ) -> None:
        if isinstance(reason, ExternalSearchToolFailureReason):
            reason_kind = reason
        elif isinstance(reason, str):
            if status_code is not None:
                raise ValueError("status_code requires a typed HTTP_STATUS reason")
            # 手書きで並べると enum に member を足したときに黙って落ちる。
            # HTTP_STATUS だけが status 付きで、それ以外は静的という契約から導く。
            static_reasons = {
                member.value
                for member in ExternalSearchToolFailureReason
                if member is not ExternalSearchToolFailureReason.HTTP_STATUS
            }
            status_prefix = f"{ExternalSearchToolFailureReason.HTTP_STATUS.value}_"
            status_suffix = reason.removeprefix(status_prefix)
            if reason in static_reasons or (
                reason.startswith(status_prefix)
                and len(status_suffix) == 3
                and status_suffix.isascii()
                and status_suffix.isdigit()
                and 100 <= int(status_suffix) <= 599
            ):
                self.reason = reason
                super().__init__(reason)
                return
            raise ValueError("unsupported external search provider failure reason")
        else:
            raise TypeError("reason must be a failure reason or safe reason code")

        if reason_kind is ExternalSearchToolFailureReason.HTTP_STATUS:
            if (
                not isinstance(status_code, int)
                or isinstance(status_code, bool)
                or not 100 <= status_code <= 599
            ):
                raise ValueError("HTTP_STATUS requires an HTTP status code")
            safe_reason = f"{reason_kind.value}_{status_code}"
        else:
            if status_code is not None:
                raise ValueError("status_code is only valid for HTTP_STATUS")
            safe_reason = reason_kind.value
        self.reason = safe_reason
        super().__init__(safe_reason)


class ExternalSearchHit(BaseModel):
    """検索 provider が返すヒット 1 件。list 順が provider rank。"""

    model_config = ConfigDict(frozen=True)

    url: SafeUrl
    title: str = Field(min_length=1)
    snippet: str | None = Field(default=None, max_length=EXTERNAL_CONTENT_MAX_CHARS)
    published_at: datetime | None = None
    source_name: str | None = None


class ExternalSearchDateFilter(BaseModel):
    """Provider非依存の半開publication日付範囲。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    start_date: date
    end_date: date

    @model_validator(mode="after")
    def _validate_order(self) -> ExternalSearchDateFilter:
        if self.start_date == date.min:
            raise ValueError("start_date must have a previous calendar day")
        if self.start_date >= self.end_date:
            raise ValueError("start_date must be before end_date")
        return self


@dataclass(frozen=True, slots=True)
class ExternalSearchToolInput:
    """External Search Toolへ渡す完成済みqueryと取得上限。"""

    query: str
    limit: int
    date_filter: ExternalSearchDateFilter | None


@dataclass(frozen=True, slots=True)
class ExternalQueryGenerationInput:
    """External Query Agent の1 attempt入力。"""

    task: ExternalResearchTask
    as_of: datetime
    target_time_window: TargetTimeWindow | None


class ExternalQueryDraft(BaseModel):
    """External Query Agent が返す未正規化query。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    queries: list[str]

    @field_validator("queries", mode="before")
    @classmethod
    def _keep_string_queries(cls, value: object) -> object:
        if isinstance(value, list):
            return [query for query in value if isinstance(query, str)]
        return value


class ExternalSearchTool(Protocol):
    @property
    def name(self) -> ExternalSearchToolName: ...

    async def search(
        self,
        input: ExternalSearchToolInput,
    ) -> list[ExternalSearchHit]: ...


@dataclass(frozen=True, slots=True)
class ExternalResearchRuntime:
    """external branchがscope内だけ借りるrole別RuntimeとToolの束。"""

    query_runtime: AgentRuntime
    reviewer_runtime: AgentRuntime
    search_tool: ExternalSearchTool


class ExternalResearchRuntimeFactory(Protocol):
    """external branch単位で資源束を貸し出すcomposition port。"""

    def activate(
        self,
    ) -> AbstractAsyncContextManager[ExternalResearchRuntime]: ...
