"""ResearchHandoffService.organize() の契約。

回答は既にストリームし終えているため、この工程の失敗でRunを失敗させない。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest

from app.agent.contract import ORGANIZED_TEXT_MAX_CHARS
from app.agent.research_handoff import (
    ResearchHandoff,
    ResearchHandoffDraft,
    ResearchHandoffInput,
    ResearchRunRecord,
    ResearchTaskRecord,
    SearchedTask,
)
from app.agent.research_handoff.agent import RESEARCH_HANDOFF_AGENT
from app.agent.research_handoff.service import ResearchHandoffService
from app.agent.runtime.contract import AgentResponseDefect, AgentResponseInvalidError
from app.analysis.ai_provider_errors import AIProviderConfigurationError

_AS_OF = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)


def _handoff(**organized: str) -> ResearchHandoff:
    return ResearchHandoff(
        updated_at=_AS_OF,
        runs=(
            ResearchRunRecord(
                as_of=_AS_OF,
                tasks=(
                    ResearchTaskRecord(research_goal="goal", executed_queries=("q",)),
                ),
            ),
        ),
        **organized,
    )


def _input(handoff: ResearchHandoff) -> ResearchHandoffInput:
    return ResearchHandoffInput(
        handoff=handoff,
        question="NVIDIAの供給は？",
        as_of=_AS_OF,
        tasks=(
            SearchedTask(
                research_goal="goal",
                executed_queries=("q",),
                external_collection="succeeded",
                hit_headlines=("見出し",),
                adopted=(("claim", "why"),),
            ),
        ),
        review_missing=("在庫水準",),
    )


class _Runtime:
    """返す値か送出する例外を1つだけ持つruntime。"""

    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.calls: list[Any] = []

    async def call(self, agent: Any, input: Any, *, attempt_number: int) -> Any:
        del agent
        self.calls.append((input, attempt_number))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _service(outcome: Any) -> tuple[ResearchHandoffService, _Runtime]:
    runtime = _Runtime(outcome)

    @asynccontextmanager
    async def scope() -> AsyncIterator[_Runtime]:
        yield runtime

    return (
        ResearchHandoffService(
            agent=RESEARCH_HANDOFF_AGENT,
            runtime_scope_factory=scope,  # type: ignore[arg-type]
        ),
        runtime,
    )


async def test_organize_replaces_the_three_texts_and_keeps_the_ledger() -> None:
    """整理は書き直され、台帳は工程が触らないため素通りする。"""
    handoff = _handoff(collected_overview="古い概観")
    service, _ = _service(
        ResearchHandoffDraft(
            collected_overview="新しい概観",
            unresolved_points="在庫水準が未確認",
            next_search_guidance="決算資料をあたる",
        )
    )

    organized = await service.organize(_input(handoff))

    assert (
        organized.collected_overview,
        organized.unresolved_points,
        organized.next_search_guidance,
    ) == ("新しい概観", "在庫水準が未確認", "決算資料をあたる")
    assert organized.runs == handoff.runs
    assert organized.updated_at == handoff.updated_at


async def test_organize_clamps_a_draft_that_overshoots_the_limit() -> None:
    """上限超過でhandoffの構築が落ちると整理が丸ごと捨たれるため、先に切る。"""
    service, _ = _service(
        ResearchHandoffDraft(collected_overview="長" * (ORGANIZED_TEXT_MAX_CHARS + 50))
    )

    organized = await service.organize(_input(_handoff()))

    assert len(organized.collected_overview) == ORGANIZED_TEXT_MAX_CHARS


@pytest.mark.parametrize(
    "failure",
    [
        AgentResponseInvalidError(AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH),
        AIProviderConfigurationError(),
    ],
)
async def test_organize_keeps_the_previous_texts_when_generation_fails(
    failure: Exception,
) -> None:
    """生成に失敗しても例外を投げず、前回の整理をそのまま残す。"""
    handoff = _handoff(
        collected_overview="前回の概観",
        unresolved_points="前回の未確認",
        next_search_guidance="前回の申し送り",
    )
    service, _ = _service(failure)

    organized = await service.organize(_input(handoff))

    assert organized == handoff


async def test_organize_calls_the_provider_once_without_retrying() -> None:
    """失敗しても前回値が残るだけなので、リトライで回答完了を遅らせない。"""
    service, runtime = _service(
        AgentResponseInvalidError(AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH)
    )

    await service.organize(_input(_handoff()))

    assert len(runtime.calls) == 1
