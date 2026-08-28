"""Research handoff package。公開名は package root から import する。

確定型は即時に出し、工程の実装は遅延 import する。planning が型だけを
読む経路で循環しないため。
"""

from importlib import import_module
from typing import Any

from app.agent.research_handoff.handoff import (
    ORGANIZED_TEXT_MAX_CHARS,
    ResearchHandoff,
    ResearchHandoffDraft,
    ResearchRunRecord,
    ResearchTaskRecord,
)

__all__ = [
    "ORGANIZED_TEXT_MAX_CHARS",
    "RESEARCH_HANDOFF_AGENT",
    "ResearchHandoff",
    "ResearchHandoffDraft",
    "ResearchHandoffInput",
    "ResearchHandoffOrganizer",
    "ResearchHandoffService",
    "ResearchRunRecord",
    "ResearchTaskRecord",
    "SearchedTask",
    "recall_research_handoff",
    "render_planning_instruction",
]

_LAZY_ATTRS: dict[str, tuple[str, str]] = {
    "RESEARCH_HANDOFF_AGENT": (
        "app.agent.research_handoff.agent",
        "RESEARCH_HANDOFF_AGENT",
    ),
    "ResearchHandoffInput": (
        "app.agent.research_handoff.handoff_input",
        "ResearchHandoffInput",
    ),
    "ResearchHandoffOrganizer": (
        "app.agent.research_handoff.service",
        "ResearchHandoffOrganizer",
    ),
    "ResearchHandoffService": (
        "app.agent.research_handoff.service",
        "ResearchHandoffService",
    ),
    "SearchedTask": ("app.agent.research_handoff.handoff_input", "SearchedTask"),
    "recall_research_handoff": (
        "app.agent.research_handoff.recall",
        "recall_research_handoff",
    ),
    "render_planning_instruction": (
        "app.agent.research_handoff.instructions",
        "render_planning_instruction",
    ),
}


def __getattr__(name: str) -> Any:
    target = _LAZY_ATTRS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr = target
    return getattr(import_module(module_name), attr)
