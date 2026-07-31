"""progress_stage 語彙が宣言箇所どうしで集合として一致することの契約テスト。

正本仕様: ``specs/agent-progress-stage-vocabulary-slice.md`` の Invariants
「工程名は6値であり、実行順に単調前進する」と Test contract「語彙の一致」。
正本は ``app/agent/contract.py`` の ``AnswerProgressStage`` だが、同じ語彙が
``AgentRunProgressStage`` (DB側StrEnum) / ``ResearchProgressStage`` (API SSoT) /
``ResearchProgressStageValue`` (射影の戻り値型) / CHECK制約
``ck_agent_runs_progress_stage`` の計5箇所に独立して宣言されている。
DB接続不要のunit test。CHECK制約は ``AgentRun.__table_args__`` から式文字列を
取り出し、正規表現でクォート付きリテラルを抽出して検証する。
"""

from __future__ import annotations

import re
import typing

from sqlalchemy import CheckConstraint

from app.agent.contract import AnswerProgressStage
from app.agent.runs.projection import ResearchProgressStageValue
from app.agent.runs.types import AgentRunProgressStage
from app.models.agent_run import AgentRun
from app.schemas.research import ResearchProgressStage

_PROGRESS_STAGE_CHECK_CONSTRAINT_NAME = "ck_agent_runs_progress_stage"

# specs/agent-progress-stage-vocabulary-slice.md の Invariants
# 「工程名は6値であり、実行順に単調前進する」由来。AnsweringRunnerが
# 実行順に報告する工程名そのものであり、production の宣言から導出しない。
_EXPECTED_PROGRESS_STAGES = frozenset(
    {
        "safety_check",
        "context_resolution",
        "planning",
        "evidence_collection",
        "evidence_review",
        "answering",
    }
)


def _check_constraint_literal_values(constraint_name: str) -> frozenset[str]:
    """CHECK制約式の文字列からクォート付きリテラルを抽出する。"""
    for arg in AgentRun.__table_args__:
        if isinstance(arg, CheckConstraint) and arg.name == constraint_name:
            return frozenset(re.findall(r"'([^']*)'", str(arg.sqltext)))
    raise AssertionError(
        f"{constraint_name} constraint not found in AgentRun.__table_args__"
    )


def test_progress_stage_vocabulary_matches_across_all_declarations() -> None:
    """5箇所の宣言がすべて仕様の6値と集合として一致する。

    1箇所だけ値を足す/削る/差し替えると、その宣言名がmismatchesに残り
    アサーションが落ちる。
    """
    declarations = {
        "AnswerProgressStage": frozenset(typing.get_args(AnswerProgressStage)),
        "AgentRunProgressStage": frozenset(
            member.value for member in AgentRunProgressStage
        ),
        "ResearchProgressStage": frozenset(typing.get_args(ResearchProgressStage)),
        "ResearchProgressStageValue": frozenset(
            typing.get_args(ResearchProgressStageValue)
        ),
        _PROGRESS_STAGE_CHECK_CONSTRAINT_NAME: _check_constraint_literal_values(
            _PROGRESS_STAGE_CHECK_CONSTRAINT_NAME
        ),
    }

    mismatches = [
        f"{name}={sorted(values)}"
        for name, values in declarations.items()
        if values != _EXPECTED_PROGRESS_STAGES
    ]

    assert not mismatches
