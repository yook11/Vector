"""Research checkpoint contracts。

過去Runが実行した外部検索の記録を`agent_runs.research_checkpoint`へ保存する
永続化契約。Checkpoint固有のcap定数は新設せず、既存正本の定数を参照する。

`ResearchTaskRecord` / `ResearchCheckpoint` / `PRIOR_RESEARCH_CHECKPOINT_LIMIT`
の本体は、RunningとPlanningの双方が参照するrun共有契約として`app.agent.contract`に
定義されている。ここではimport siteを変えないための re-export のみ行う。
"""

from __future__ import annotations

from app.agent.contract import (
    PRIOR_RESEARCH_CHECKPOINT_LIMIT,
    ResearchCheckpoint,
    ResearchTaskRecord,
)

__all__ = [
    "PRIOR_RESEARCH_CHECKPOINT_LIMIT",
    "ResearchCheckpoint",
    "ResearchTaskRecord",
]
