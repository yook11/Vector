"""Research checkpoint contracts。

過去Runが実行した外部検索の記録を`agent_runs.research_checkpoint`へ保存する
永続化契約。Checkpoint固有のcap定数は新設せず、既存正本の定数を参照する。
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

from app.agent.evidence_collection.evidence_review.contract import (
    EVIDENCE_REVIEW_ADOPTION_LIMIT,
    EVIDENCE_REVIEW_MISSING_LIMIT,
)
from app.agent.evidence_collection.external_search.contract import (
    EVIDENCE_CLAIM_MAX_CHARS,
    EXTERNAL_QUERY_MAX_CHARS,
    EXTERNAL_TASK_QUERY_LIMIT,
    MISSING_ITEM_MAX_CHARS,
)
from app.agent.planning.contract import RESEARCH_GOAL_MAX_CHARS, RESEARCH_TASK_LIMIT

__all__ = ["ResearchCheckpoint", "ResearchTaskRecord"]

_ExecutedQuery = Annotated[
    str,
    StringConstraints(min_length=1, max_length=EXTERNAL_QUERY_MAX_CHARS),
]
_AdoptedClaim = Annotated[str, StringConstraints(max_length=EVIDENCE_CLAIM_MAX_CHARS)]
_UnresolvedItem = Annotated[str, StringConstraints(max_length=MISSING_ITEM_MAX_CHARS)]


class ResearchTaskRecord(BaseModel):
    """1 research taskの調査記録。executed_queriesが空になるtaskは記録しない。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    research_goal: str = Field(min_length=1, max_length=RESEARCH_GOAL_MAX_CHARS)
    # provider呼び出しに成功した外部queryのみ。min 1件を型で強制する。
    executed_queries: tuple[_ExecutedQuery, ...] = Field(
        min_length=1,
        max_length=EXTERNAL_TASK_QUERY_LIMIT,
    )
    # 外部検索から採用されたclaim。空 = 有用な候補なし。
    adopted_claims: tuple[_AdoptedClaim, ...] = Field(
        max_length=EVIDENCE_REVIEW_ADOPTION_LIMIT,
    )


class ResearchCheckpoint(BaseModel):
    """Runが実行した外部検索の決定的な記録。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    as_of: AwareDatetime
    # min 1件。0件になるRunはcolumnをNULLにする(builderがNoneを返す)。
    tasks: tuple[ResearchTaskRecord, ...] = Field(
        min_length=1,
        max_length=RESEARCH_TASK_LIMIT,
    )
    # Evidence Reviewerのmissingのverbatim copy。Run全体で1本。
    unresolved_after_search: tuple[_UnresolvedItem, ...] = Field(
        max_length=EVIDENCE_REVIEW_MISSING_LIMIT,
    )
