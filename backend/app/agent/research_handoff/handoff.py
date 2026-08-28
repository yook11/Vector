"""確定した調査の申し送り。

この工程の成果物であり、threadへ保存し次のRunのplannerが読む。
"""

from __future__ import annotations

from typing import Annotated, Final, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
)

from app.agent.contract import (
    EXTERNAL_QUERY_MAX_CHARS,
    EXTERNAL_TASK_QUERY_LIMIT,
    RESEARCH_GOAL_MAX_CHARS,
    RESEARCH_TASK_LIMIT,
)

__all__ = [
    "ORGANIZED_TEXT_MAX_CHARS",
    "ResearchHandoff",
    "ResearchHandoffDraft",
    "ResearchRunRecord",
    "ResearchTaskRecord",
]

# 整理1本あたりの上限。thread全体を1本へ畳んだ結果であり、Run数によらず一定に保つ。
ORGANIZED_TEXT_MAX_CHARS: Final[int] = 600

_ExecutedQuery = Annotated[
    str,
    StringConstraints(min_length=1, max_length=EXTERNAL_QUERY_MAX_CHARS),
]
_OrganizedText = Annotated[
    str,
    StringConstraints(max_length=ORGANIZED_TEXT_MAX_CHARS),
]


class ResearchHandoffDraft(BaseModel):
    """LLMの構造化出力。制約は確定型へ落とすときに課す。"""

    model_config = ConfigDict(frozen=True)

    collected_overview: str = ""
    unresolved_points: str = ""
    next_search_guidance: str = ""


class ResearchTaskRecord(BaseModel):
    """1 research taskの調査台帳。executed_queriesが空になるtaskは記録しない。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    research_goal: str = Field(min_length=1, max_length=RESEARCH_GOAL_MAX_CHARS)
    # provider呼び出しに成功した外部queryのみ。min 1件を型で強制する。
    executed_queries: tuple[_ExecutedQuery, ...] = Field(
        min_length=1,
        max_length=EXTERNAL_TASK_QUERY_LIMIT,
    )


class ResearchRunRecord(BaseModel):
    """1 Runが何を狙って何を叩いたかの台帳。

    次の調査は実行済みqueryを文字列として読むため、LLMに畳ませず決定的に積む。
    何が得られたかはResearchHandoffの整理側が持つ。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    as_of: AwareDatetime
    # min 1件。0件になるRunは記録しない。
    tasks: tuple[ResearchTaskRecord, ...] = Field(
        min_length=1,
        max_length=RESEARCH_TASK_LIMIT,
    )


class ResearchHandoff(BaseModel):
    """threadが積み上げた、次のRunへの調査の申し送り。

    台帳(runs)には上限を置かず、search Runごとに追記する。整理の3本は
    Runごとに積まず、この工程が毎回1本へ書き直す。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    updated_at: AwareDatetime
    # 古い順。1件目はhandoffを最初に書いたRun。
    runs: tuple[ResearchRunRecord, ...] = Field(min_length=1)
    collected_overview: _OrganizedText = ""
    unresolved_points: _OrganizedText = ""
    next_search_guidance: _OrganizedText = ""

    @classmethod
    def with_run(
        cls,
        *,
        previous: Self | None,
        record: ResearchRunRecord,
    ) -> Self:
        """今回の台帳を末尾へ積む。整理は前回の値をそのまま引き継ぐ。"""
        if previous is None:
            return cls(updated_at=record.as_of, runs=(record,))
        return cls(
            updated_at=record.as_of,
            runs=previous.runs + (record,),
            collected_overview=previous.collected_overview,
            unresolved_points=previous.unresolved_points,
            next_search_guidance=previous.next_search_guidance,
        )

    def from_draft(self, draft: ResearchHandoffDraft) -> Self:
        """下書きを正規化して、整理3本だけを差し替えた申し送りを返す。"""
        return type(self)(
            updated_at=self.updated_at,
            runs=self.runs,
            collected_overview=_clean(draft.collected_overview),
            unresolved_points=_clean(draft.unresolved_points),
            next_search_guidance=_clean(draft.next_search_guidance),
        )


def _clean(value: str) -> str:
    return value.strip()[:ORGANIZED_TEXT_MAX_CHARS].strip()
