"""ResearchHandoffをplanner promptへ差し込む投影。

handoffは調査計画の文脈であり回答の事実根拠ではない。投影先をplanner 1本に
限ることで、回答工程へ渡らないことを構造で保つ。
"""

from __future__ import annotations

from typing import Final

from app.agent.contract import ResearchHandoff, ResearchRunRecord
from app.analysis.prompt_safety import sanitize_for_untrusted_block

__all__ = ["render_planning_instruction"]

_RESEARCH_HANDOFF_TEMPLATE: Final[str] = """
# Research Handoff
同じthreadでこれまでに行った調査の申し送り。

<untrusted_prior_research>
## 実行した調査(古い順)
{records}
{organized}</untrusted_prior_research>
"""

_ORGANIZED_SECTION_TEMPLATE: Final[str] = """
## {label}
{body}
"""


def render_planning_instruction(handoff: ResearchHandoff | None) -> str:
    """planner promptへ差し込む文脈。handoffが無ければ空文字を返す。"""
    if handoff is None:
        return ""
    # HTMLではないLLM promptであり、外部入力は境界用sanitizerを通す。
    # nosemgrep: python.django.security.injection.raw-html-format.raw-html-format  # noqa: E501
    return _RESEARCH_HANDOFF_TEMPLATE.format(
        records=_render_runs(handoff.runs),
        organized=_render_organized(handoff),
    )


def _render_organized(handoff: ResearchHandoff) -> str:
    """書かれていない整理は節ごと出さない。"""
    sections = (
        ("集まったもの", handoff.collected_overview),
        ("確認できていないこと", handoff.unresolved_points),
        ("次の調査への申し送り", handoff.next_search_guidance),
    )
    return "".join(
        _ORGANIZED_SECTION_TEMPLATE.format(
            label=label,
            body=sanitize_for_untrusted_block(body),
        )
        for label, body in sections
        if body
    )


def _render_runs(runs: tuple[ResearchRunRecord, ...]) -> str:
    return "\n\n".join(_render_run(record) for record in runs)


def _render_run(record: ResearchRunRecord) -> str:
    lines = [f"[調査時点: {record.as_of.isoformat()}]"]
    for task in record.tasks:
        lines.append(
            f"research_goal: {sanitize_for_untrusted_block(task.research_goal)}"
        )
        lines.append("実行したquery:")
        lines.extend(
            f"- {sanitize_for_untrusted_block(query)}"
            for query in task.executed_queries
        )
    return "\n".join(lines)
