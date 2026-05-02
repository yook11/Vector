"""LLM 入力用の記事 VO。

Stage 1 抽出済の (article_id, title_ja, summary_ja) のみを LLM に渡す。
原文 / category / topic / entity_list 等は briefing prompt の品質に貢献せず、
コストとプロンプトインジェクション面での攻撃面を増やすだけなので渡さない
(`project_phase1b_briefing_design.md`)。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ArticleInput(BaseModel):
    """LLM への記事入力 1 件。"""

    model_config = ConfigDict(frozen=True)

    id: int = Field(gt=0)
    title_ja: str = Field(min_length=1)
    summary_ja: str = Field(min_length=1)
