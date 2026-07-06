"""External search の境界型・port 契約・構造 cap 定数。

runner / service / adapter が共有する frozen model と Protocol をここで保証する。
自由記述欄の clamp は from_raw factory で行い、model validator は
「factory を通れば違反しない」不変条件として保持する。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent.contract import ExternalResearchTask
from app.shared.security.safe_url import SafeUrl

__all__ = [
    "CANDIDATE_SNIPPET_MAX_CHARS",
    "EVIDENCE_CLAIM_MAX_CHARS",
    "EVIDENCE_WHY_SELECTED_MAX_CHARS",
    "EXTERNAL_QUERY_MAX_CHARS",
    "EXTERNAL_SEARCH_AGENT_HARD_LIMIT",
    "EXTERNAL_SEARCH_CANDIDATES_PER_QUERY",
    "EXTERNAL_SEARCH_CANDIDATE_POOL_LIMIT_PER_TASK",
    "EXTERNAL_SEARCH_EVIDENCE_LIMIT_PER_TASK",
    "EXTERNAL_SEARCH_MISSING_LIMIT_PER_TASK",
    "EXTERNAL_TASK_QUERY_LIMIT",
    "EvidenceSelection",
    "EvidenceSelectionResult",
    "EvidenceSelector",
    "ExternalEvidenceSelectorError",
    "ExternalQueryGenerationError",
    "ExternalSearchCandidate",
    "ExternalSearchEvidence",
    "ExternalSearchOutcome",
    "ExternalSearchProviderError",
    "ExternalSearchRequest",
    "ExternalSearchRunResult",
    "ExternalSearchRunner",
    "MISSING_ITEM_MAX_CHARS",
    "QueryGenerator",
    "ResearchTaskReport",
    "ResearchTaskStatus",
    "SearchProvider",
]

EXTERNAL_SEARCH_AGENT_HARD_LIMIT = 3
EXTERNAL_TASK_QUERY_LIMIT = 3
EXTERNAL_QUERY_MAX_CHARS = 200
EXTERNAL_SEARCH_CANDIDATES_PER_QUERY = 10
EXTERNAL_SEARCH_CANDIDATE_POOL_LIMIT_PER_TASK = 20
EXTERNAL_SEARCH_EVIDENCE_LIMIT_PER_TASK = 5
EXTERNAL_SEARCH_MISSING_LIMIT_PER_TASK = 5
CANDIDATE_SNIPPET_MAX_CHARS = 500
EVIDENCE_CLAIM_MAX_CHARS = 300
EVIDENCE_WHY_SELECTED_MAX_CHARS = 300
MISSING_ITEM_MAX_CHARS = 200

ResearchTaskStatus = Literal[
    "succeeded",
    "query_generation_failed",
    "provider_failed",
    "selector_failed",
]


class ExternalQueryGenerationError(Exception):
    """Query generator adapter が分類済み失敗として raise する境界 error。"""

    reason: str | None

    def __init__(self, message: str = "", *, reason: str | None = None) -> None:
        super().__init__(message or reason or "")
        self.reason = reason


class ExternalSearchProviderError(Exception):
    """Search provider adapter が分類済み失敗として raise する境界 error。"""


class ExternalEvidenceSelectorError(Exception):
    """Evidence selector adapter が分類済み失敗として raise する境界 error。"""

    reason: str | None

    def __init__(self, message: str = "", *, reason: str | None = None) -> None:
        super().__init__(message or reason or "")
        self.reason = reason


class ExternalSearchCandidate(BaseModel):
    """検索 provider が返す候補 1 件。list 順が provider rank。"""

    model_config = ConfigDict(frozen=True)

    url: SafeUrl
    title: str = Field(min_length=1)
    snippet: str | None = Field(default=None, max_length=CANDIDATE_SNIPPET_MAX_CHARS)
    published_at: datetime | None = None
    source_name: str | None = None


class QueryGenerator(Protocol):
    async def generate(
        self,
        *,
        task: ExternalResearchTask,
        as_of: datetime,
        target_time_window: str | None,
    ) -> list[str]: ...


class SearchProvider(Protocol):
    async def search(
        self,
        query: str,
        *,
        limit: int,
    ) -> list[ExternalSearchCandidate]: ...


class EvidenceSelection(BaseModel):
    """selector が返す選別 1 件。URL は返さず index で pool を参照する。"""

    model_config = ConfigDict(frozen=True)

    candidate_index: int = Field(ge=0)
    claim: str = Field(min_length=1, max_length=EVIDENCE_CLAIM_MAX_CHARS)
    why_selected: str = Field(
        min_length=1,
        max_length=EVIDENCE_WHY_SELECTED_MAX_CHARS,
    )


class EvidenceSelectionResult(BaseModel):
    """selector が返す選別結果。自由記述欄の cap は factory で丸める。"""

    model_config = ConfigDict(frozen=True)

    selections: list[EvidenceSelection] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)

    @classmethod
    def from_raw(
        cls,
        *,
        selections: Sequence[EvidenceSelection | Mapping[str, object]],
        missing: Sequence[str],
    ) -> EvidenceSelectionResult:
        clamped_selections: list[EvidenceSelection] = []
        for selection in selections:
            if isinstance(selection, EvidenceSelection):
                clamped_selections.append(selection)
                continue
            item = dict(selection)
            if "claim" in item:
                item["claim"] = _truncate_text(item["claim"], EVIDENCE_CLAIM_MAX_CHARS)
            if "why_selected" in item:
                item["why_selected"] = _truncate_text(
                    item["why_selected"],
                    EVIDENCE_WHY_SELECTED_MAX_CHARS,
                )
            clamped_selections.append(EvidenceSelection.model_validate(item))

        return cls(
            selections=clamped_selections,
            missing=_clamp_missing(missing),
        )

    @model_validator(mode="after")
    def _validate_missing_caps(self) -> EvidenceSelectionResult:
        if len(self.missing) > EXTERNAL_SEARCH_MISSING_LIMIT_PER_TASK:
            raise ValueError("missing exceeds external search missing limit")
        if any(len(item) > MISSING_ITEM_MAX_CHARS for item in self.missing):
            raise ValueError("missing item exceeds max length")
        return self


class EvidenceSelector(Protocol):
    async def select(
        self,
        *,
        task: ExternalResearchTask,
        candidates: list[ExternalSearchCandidate],
        as_of: datetime,
    ) -> EvidenceSelectionResult: ...


class ResearchTaskReport(BaseModel):
    """task 単位の検索実行内容と失敗分類。"""

    model_config = ConfigDict(frozen=True)

    task_index: int = Field(ge=0)
    collection_goal: str = Field(min_length=1)
    generated_queries: list[str] = Field(default_factory=list)
    status: ResearchTaskStatus
    provider_failed_query_count: int = Field(default=0, ge=0)
    candidate_count: int = Field(default=0, ge=0)
    evidence_count: int = Field(default=0, ge=0)
    dropped_selection_count: int = Field(default=0, ge=0)
    selector_failure_reason: str | None = None
    missing: list[str] = Field(default_factory=list)

    @classmethod
    def from_raw(
        cls,
        *,
        task_index: int,
        collection_goal: str,
        generated_queries: list[str] | None = None,
        status: ResearchTaskStatus,
        provider_failed_query_count: int = 0,
        candidate_count: int = 0,
        evidence_count: int = 0,
        dropped_selection_count: int = 0,
        selector_failure_reason: str | None = None,
        missing: list[str] | None = None,
    ) -> ResearchTaskReport:
        return cls(
            task_index=task_index,
            collection_goal=collection_goal,
            generated_queries=generated_queries or [],
            status=status,
            provider_failed_query_count=provider_failed_query_count,
            candidate_count=candidate_count,
            evidence_count=evidence_count,
            dropped_selection_count=dropped_selection_count,
            selector_failure_reason=selector_failure_reason,
            missing=_clamp_missing(missing or []),
        )

    @model_validator(mode="after")
    def _validate_report_caps(self) -> ResearchTaskReport:
        if len(self.generated_queries) > EXTERNAL_TASK_QUERY_LIMIT:
            raise ValueError("generated queries exceed external query limit")
        if any(
            len(query) > EXTERNAL_QUERY_MAX_CHARS for query in self.generated_queries
        ):
            raise ValueError("generated query exceeds max length")
        if len(self.missing) > EXTERNAL_SEARCH_MISSING_LIMIT_PER_TASK:
            raise ValueError("missing exceeds external search missing limit")
        if any(len(item) > MISSING_ITEM_MAX_CHARS for item in self.missing):
            raise ValueError("missing item exceeds max length")
        if self.evidence_count > EXTERNAL_SEARCH_EVIDENCE_LIMIT_PER_TASK:
            raise ValueError("evidence count exceeds external evidence limit")
        return self


class ExternalSearchEvidence(BaseModel):
    """外部検索 runner が main agent へ渡す URL 根拠候補。"""

    model_config = ConfigDict(frozen=True)

    source_ref: str = Field(min_length=1)
    task_index: int = Field(ge=0)
    claim: str = Field(min_length=1, max_length=EVIDENCE_CLAIM_MAX_CHARS)
    why_selected: str = Field(
        min_length=1,
        max_length=EVIDENCE_WHY_SELECTED_MAX_CHARS,
    )
    url: SafeUrl
    title: str = Field(min_length=1)
    snippet: str | None = None
    published_at: datetime | None = None
    source_name: str | None = None


class ExternalSearchRunResult(BaseModel):
    """runner が service へ返す処理結果。集約 policy は含めない。"""

    model_config = ConfigDict(frozen=True)

    evidence: list[ExternalSearchEvidence] = Field(default_factory=list)
    task_reports: list[ResearchTaskReport] = Field(default_factory=list)


class ExternalSearchRequest(BaseModel):
    """external search runner に渡す実行済み検索計画。"""

    model_config = ConfigDict(frozen=True)

    tasks: list[ExternalResearchTask]
    requested_agent_count: int | None = None
    effective_agent_count: int = Field(ge=0)
    as_of: datetime
    target_time_window: str | None = None
    hard_agent_limit: int = Field(default=EXTERNAL_SEARCH_AGENT_HARD_LIMIT, ge=1)


class ExternalSearchOutcome(BaseModel):
    """外部検索の実行結果と、丸め後の実行ポリシー。"""

    model_config = ConfigDict(frozen=True)

    tasks: list[ExternalResearchTask] = Field(default_factory=list)
    evidence: list[ExternalSearchEvidence] = Field(default_factory=list)
    task_reports: list[ResearchTaskReport] = Field(default_factory=list)
    deduplicated_evidence_count: int = Field(default=0, ge=0)
    requested_agent_count: int | None = None
    effective_agent_count: int = Field(default=0, ge=0)
    hard_agent_limit: int = Field(default=EXTERNAL_SEARCH_AGENT_HARD_LIMIT, ge=1)

    @model_validator(mode="after")
    def _validate_task_correspondence(self) -> ExternalSearchOutcome:
        task_count = len(self.tasks)
        report_indexes = [report.task_index for report in self.task_reports]
        if len(report_indexes) != task_count:
            raise ValueError("task report count must match task count")
        if set(report_indexes) != set(range(task_count)):
            raise ValueError("task reports must cover each task exactly once")

        if any(
            evidence.task_index < 0 or evidence.task_index >= task_count
            for evidence in self.evidence
        ):
            raise ValueError("evidence task_index must reference an existing task")

        source_refs = [evidence.source_ref for evidence in self.evidence]
        if len(source_refs) != len(set(source_refs)):
            raise ValueError("external evidence source_ref must be unique")

        reported_evidence_count = sum(
            report.evidence_count for report in self.task_reports
        )
        if reported_evidence_count != (
            len(self.evidence) + self.deduplicated_evidence_count
        ):
            raise ValueError("reported evidence count must match outcome evidence")
        return self


class ExternalSearchRunner(Protocol):
    """DeepSeek / search provider 実装の手前に置く runner 境界。"""

    async def search(
        self,
        request: ExternalSearchRequest,
    ) -> ExternalSearchRunResult: ...


def _clamp_missing(missing: Sequence[str]) -> list[str]:
    return [
        _truncate_text(item, MISSING_ITEM_MAX_CHARS)
        for item in missing[:EXTERNAL_SEARCH_MISSING_LIMIT_PER_TASK]
    ]


def _truncate_text(value: object, max_chars: int) -> str:
    return str(value)[:max_chars]
