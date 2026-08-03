"""読出したResearchCheckpoint JSONBの検証。

DBへ保存済みのJSONBは書込み後のschema変更で無効になりうるため、Plannerへ渡す前に
必ず`model_validate`を通し、失敗した要素だけを個別に握って除外する(注入フロー3)。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

import logfire

from app.agent.research_checkpoint.contract import ResearchCheckpoint

__all__ = ["recall_research_checkpoints"]

_INVALID_CHECKPOINT_FAILURE_CODE: Final[str] = "invalid_checkpoint_skipped"


def recall_research_checkpoints(
    raw_checkpoints: Sequence[Mapping[str, Any]],
) -> tuple[ResearchCheckpoint, ...]:
    """新しい順の生JSONBを検証し、無効な要素だけを順序を保ったまま除外する。"""
    validated: list[ResearchCheckpoint] = []
    invalid_count = 0
    for raw_checkpoint in raw_checkpoints:
        try:
            validated.append(ResearchCheckpoint.model_validate(raw_checkpoint))
        except Exception:
            invalid_count += 1
    if invalid_count:
        logfire.warning(
            "research_checkpoint_recall_invalid_skipped",
            invalid_count=invalid_count,
            failure_code=_INVALID_CHECKPOINT_FAILURE_CODE,
        )
    return tuple(validated)
