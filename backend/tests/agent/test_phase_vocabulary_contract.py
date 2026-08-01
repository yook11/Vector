"""AgentPhase語彙がstage語彙どうしで集合として一致することの契約テスト。

正本仕様: ``specs/agent-phase-span-vocabulary-slice.md`` の Invariants
「phaseはそのspanがどの工程に属するかを表す」(値はstage語彙と同じ6値) と
Test contract「phase 属性」。
"""

from __future__ import annotations

import typing

from app.agent.contract import AnswerProgressStage
from app.agent.phase_span import AgentPhase

# specs/agent-phase-span-vocabulary-slice.md の Invariants
# 「値はstage語彙と同じ6値(safety_check / context_resolution / planning /
# evidence_collection / evidence_review / answering)とする」由来。
# AgentPhaseの宣言から導出しない。
_EXPECTED_AGENT_PHASES = frozenset(
    {
        "safety_check",
        "context_resolution",
        "planning",
        "evidence_collection",
        "evidence_review",
        "answering",
    }
)


def test_agent_phase_vocabulary_matches_stage_vocabulary_and_the_spec() -> None:
    """``AgentPhase``が仕様の6値、および``AnswerProgressStage``と集合として一致する。

    1箇所だけ値を足す/削る/差し替えると、その宣言名がmismatchesに残り
    アサーションが落ちる。
    """
    declarations = {
        "AgentPhase": frozenset(typing.get_args(AgentPhase)),
        "AnswerProgressStage": frozenset(typing.get_args(AnswerProgressStage)),
    }

    mismatches = [
        f"{name}={sorted(values)}"
        for name, values in declarations.items()
        if values != _EXPECTED_AGENT_PHASES
    ]

    assert not mismatches
