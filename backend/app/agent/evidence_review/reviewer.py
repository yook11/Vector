"""Run内の全taskの候補を1回で精査し、Run全体の根拠と不足を見極める
EvidenceReviewer。

DB / Redis / HTTP client の生成は composition が所有し、Reviewer は渡された
Runtime だけを使う(責任境界: Reviewer は infrastructure の構築を知らない)。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from pydantic import ValidationError

from app.agent.evidence_collection.contract import CollectedTask
from app.agent.evidence_review.agent import EVIDENCE_REVIEWER_AGENT
from app.agent.evidence_review.policy import (
    EVIDENCE_REVIEW_TIMEOUT_SECONDS,
    REVIEWER_ERROR_REASON,
    REVIEWER_TIMEOUT_REASON,
)
from app.agent.evidence_review.preparation import (
    EvidenceReviewInput,
    EvidenceReviewPreparation,
)
from app.agent.evidence_review.result import (
    AnswerEvidence,
    EvidenceReviewerResponse,
    EvidenceReviewOutcome,
)
from app.agent.phase_span import agent_phase
from app.agent.runtime.contract import (
    AgentResponseDefect,
    AgentResponseInvalidError,
    AgentRuntime,
)
from app.analysis.ai_provider_errors import AIProviderError

__all__ = ["EvidenceReviewer"]


@dataclass(frozen=True, slots=True)
class EvidenceReviewer:
    """Run内の全taskの候補を1回の入力で精査する。収集と回答生成は持たない。"""

    async def review(
        self,
        *,
        tasks: list[CollectedTask],
        as_of: datetime,
        reviewer_runtime: AgentRuntime,
    ) -> EvidenceReviewOutcome:
        preparation = EvidenceReviewPreparation.from_tasks(tasks)
        review_input = EvidenceReviewInput(
            task_groups=preparation.task_groups,
            as_of=as_of,
        )
        failure_reason: str | None = None
        with agent_phase(
            phase="evidence_review",
            agent_name=EVIDENCE_REVIEWER_AGENT.name,
        ):
            for attempt_number in range(1, 3):
                try:
                    draft = await asyncio.wait_for(
                        reviewer_runtime.invoke(
                            EVIDENCE_REVIEWER_AGENT,
                            review_input,
                            attempt_number=attempt_number,
                        ),
                        timeout=EVIDENCE_REVIEW_TIMEOUT_SECONDS,
                    )
                except AgentResponseInvalidError as exc:
                    failure_reason = exc.defect.value
                    continue
                except AIProviderError as exc:
                    failure_reason = _provider_failure_reason(exc)
                    continue
                except TimeoutError:
                    failure_reason = REVIEWER_TIMEOUT_REASON
                    continue

                try:
                    reviewer_response = EvidenceReviewerResponse.from_draft(draft)
                except ValidationError:
                    failure_reason = AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH.value
                    continue

                answer_evidence = AnswerEvidence.from_reviewer_response(
                    preparation=preparation,
                    reviewer_response=reviewer_response,
                )
                return EvidenceReviewOutcome(
                    answer_evidence=answer_evidence,
                    missing=reviewer_response.missing,
                    failure_reason=None,
                )
        return EvidenceReviewOutcome(
            answer_evidence=AnswerEvidence(),
            missing=(),
            failure_reason=failure_reason,
        )


def _provider_failure_reason(exc: AIProviderError) -> str:
    reason = getattr(exc, "reason", None)
    if isinstance(reason, StrEnum):
        return reason.value

    code = getattr(exc, "CODE", None)
    if isinstance(code, str):
        return code

    return REVIEWER_ERROR_REASON
