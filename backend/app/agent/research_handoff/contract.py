"""research_handoff工程の契約。

工程の成果物はResearchHandoffそのもので、整理層だけを包む型は作らない。
LLMが書き直すのは整理3本だけであり、台帳(runs)はrunnerが決定的に積む。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from app.agent.contract import ResearchHandoff

__all__ = [
    "HandoffMaterial",
    "HandoffOrganizerInput",
    "HandoffTaskMaterial",
    "ResearchHandoffDraft",
    "ResearchHandoffOrganizer",
]


@dataclass(frozen=True, slots=True)
class HandoffTaskMaterial:
    """1 taskで何を狙い、何を叩き、何が手に入る状態だったか。"""

    research_goal: str
    executed_queries: tuple[str, ...]
    # 外部収集の結末。「情報が無かった」と「検索できなかった」の区別に要る。
    external_collection: str
    # 採用されなかったものも含むヒット記事。本文は渡さない。
    hit_headlines: tuple[str, ...]
    # 採用されたevidenceのclaimと、reviewerがそれを選んだ理由。
    adopted: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class HandoffMaterial:
    """整理を書き直すためにLLMへ見せる、今回のRunの素材。"""

    question: str
    as_of: datetime
    tasks: tuple[HandoffTaskMaterial, ...]
    review_missing: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HandoffOrganizerInput:
    """整理の1回に渡す実行時input。書き直す対象と、その材料。"""

    handoff: ResearchHandoff
    material: HandoffMaterial


class ResearchHandoffDraft(BaseModel):
    """LLMの構造化出力を素朴にparseしたdraft(正規化前は無制約)。"""

    model_config = ConfigDict(frozen=True)

    collected_overview: str = ""
    unresolved_points: str = ""
    next_search_guidance: str = ""


class ResearchHandoffOrganizer(Protocol):
    """台帳を積み終えたhandoffの整理3本を書き直す。"""

    async def organize(
        self,
        *,
        handoff: ResearchHandoff,
        material: HandoffMaterial,
    ) -> ResearchHandoff: ...
