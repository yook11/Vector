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

from app.agent.evidence_collection.evidence_review.agent import EVIDENCE_REVIEWER_AGENT
from app.agent.evidence_collection.evidence_review.contract import (
    EvidenceReviewInput,
    EvidenceReviewOutcome,
    ReviewTaskCandidates,
)
from app.agent.evidence_collection.evidence_review.policy import (
    EVIDENCE_REVIEW_TIMEOUT_SECONDS,
    REVIEWER_TIMEOUT_REASON,
    build_review_evidence,
    build_review_task_groups,
    finalize_review_draft,
    resolve_reviewer_failure_reason,
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
        tasks: list[ReviewTaskCandidates],
        content_requirements: tuple[str, ...],
        as_of: datetime,
        reviewer_runtime: AgentRuntime,
    ) -> EvidenceReviewOutcome:
        review_input = EvidenceReviewInput(
            task_groups=build_review_task_groups(tasks),
            content_requirements=content_requirements,
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
                    selection_result = finalize_review_draft(draft)
                except ValidationError:
                    failure_reason = AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH.value
                    continue

                internal_evidence, external_evidence, dropped = build_review_evidence(
                    tasks=tasks,
                    selection_result=selection_result,
                )
                return EvidenceReviewOutcome(
                    internal_evidence=internal_evidence,
                    external_evidence=external_evidence,
                    missing=selection_result.missing,
                    dropped_selection_count=dropped,
                    failure_reason=None,
                )
        return EvidenceReviewOutcome(
            internal_evidence=[],
            external_evidence=[],
            missing=[],
            dropped_selection_count=0,
            failure_reason=failure_reason,
        )


def _provider_failure_reason(exc: AIProviderError) -> str:
    reason = getattr(exc, "reason", None)
    reason_value = reason.value if isinstance(reason, StrEnum) else None
    code = getattr(exc, "CODE", None)
    return resolve_reviewer_failure_reason(
        reason=reason_value,
        code=code if isinstance(code, str) else None,
    )
