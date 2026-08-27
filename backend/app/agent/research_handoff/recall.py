"""読出したResearchHandoff JSONBの検証。

DBへ保存済みのJSONBは書込み後のschema変更で無効になりうるため、Plannerへ渡す前に
必ず`model_validate`を通す。失敗したhandoffは無かったものとして扱う。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

import logfire

from app.agent.contract import ResearchHandoff

__all__ = ["recall_research_handoff"]

_INVALID_HANDOFF_FAILURE_CODE: Final[str] = "invalid_handoff_skipped"


def recall_research_handoff(
    raw_handoff: Mapping[str, Any] | None,
) -> ResearchHandoff | None:
    """生JSONBを検証する。無効なhandoffはNoneにして次Runを素の状態で始める。"""
    if raw_handoff is None:
        return None
    try:
        return ResearchHandoff.model_validate(raw_handoff)
    except Exception:
        logfire.warning(
            "research_handoff_recall_invalid_skipped",
            failure_code=_INVALID_HANDOFF_FAILURE_CODE,
        )
        return None
