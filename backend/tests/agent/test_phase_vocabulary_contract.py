"""AgentPhase語彙と進捗stage語彙の包含関係の契約テスト。

phaseはそのspanがどの工程に属するかを表す、という約束を確かめる。
"""

from __future__ import annotations

import typing

from app.agent.contract import AnsweringStage
from app.agent.phase_span import AgentPhase


def test_every_progress_stage_is_an_agent_phase() -> None:
    """ユーザーへ見せる進捗stageは、必ずspanのphaseとしても存在する。

    逆は成り立たない。回答確定前の後処理のように、工程として観測はするが
    進捗としては見せないものがある。
    """
    progress_stages = frozenset(typing.get_args(AnsweringStage))
    agent_phases = frozenset(typing.get_args(AgentPhase))

    assert progress_stages <= agent_phases
