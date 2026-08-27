"""整理のLLM出力と、それをhandoffへ落とすまでの正規化。

上限超過でhandoffの構築が落ちると整理が丸ごと捨たれるため、clampはここで
済ませる。台帳はdraftに含まれないため、この経路では書き換わらない。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.agent.contract import ORGANIZED_TEXT_MAX_CHARS, ResearchHandoff

__all__ = ["ResearchHandoffDraft", "organized_handoff_from_draft"]


class ResearchHandoffDraft(BaseModel):
    """LLMの構造化出力を素朴にparseしたdraft(正規化前は無制約)。"""

    model_config = ConfigDict(frozen=True)

    collected_overview: str = ""
    unresolved_points: str = ""
    next_search_guidance: str = ""


def organized_handoff_from_draft(
    *,
    handoff: ResearchHandoff,
    draft: ResearchHandoffDraft,
) -> ResearchHandoff:
    """整理3本だけを差し替えたhandoffを返す。"""
    return ResearchHandoff(
        updated_at=handoff.updated_at,
        runs=handoff.runs,
        collected_overview=_clean(draft.collected_overview),
        unresolved_points=_clean(draft.unresolved_points),
        next_search_guidance=_clean(draft.next_search_guidance),
    )


def _clean(value: str) -> str:
    return value.strip()[:ORGANIZED_TEXT_MAX_CHARS].strip()
