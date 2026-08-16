"""Run内の全taskの選択肢を1回で精査し、Run全体の根拠と不足を見極める
EvidenceReviewer。

DB / Redis / HTTP client の生成は composition が所有し、Reviewer は渡された
Runtime だけを使う(責任境界: Reviewer は infrastructure の構築を知らない)。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime

from pydantic import ValidationError

from app.agent.evidence_collection.contract import CollectedTask
from app.agent.evidence_review.agent import EVIDENCE_REVIEWER_AGENT
from app.agent.evidence_review.answer_evidence import (
    AnswerEvidence,
    EvidenceRunCompleted,
    EvidenceRunFailed,
    EvidenceRunResult,
)
from app.agent.evidence_review.preparation import (
    EvidenceReviewInput,
    EvidenceReviewPreparation,
)
from app.agent.evidence_review.selection import EvidenceReviewerResponse
from app.agent.phase_span import agent_phase
from app.agent.runtime.contract import (
    AgentResponseDefect,
    AgentResponseInvalidError,
    AgentRuntime,
)
from app.analysis.ai_provider_errors import (
    AIProviderContentError,
    AIProviderStateError,
)

__all__ = ["EvidenceReviewer"]

_MAX_REVIEW_ATTEMPTS = 2
_REVIEW_ATTEMPT_TIMEOUT_SECONDS = 30
_REVIEW_ATTEMPT_TIMEOUT_REASON = "reviewer_timeout"


@dataclass(frozen=True, slots=True)
class EvidenceReviewer:
    """Run内の全taskの選択肢を1回の入力で精査する。収集と回答生成は持たない。"""

    async def review(
        self,
        *,
        tasks: list[CollectedTask],
        as_of: datetime,
        reviewer_runtime: AgentRuntime,
    ) -> EvidenceRunResult:
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
            for attempt_number in range(1, _MAX_REVIEW_ATTEMPTS + 1):
                try:
                    draft = await asyncio.wait_for(
                        reviewer_runtime.call(
                            EVIDENCE_REVIEWER_AGENT,
                            review_input,
                            attempt_number=attempt_number,
                        ),
                        timeout=_REVIEW_ATTEMPT_TIMEOUT_SECONDS,
                    )
                except AgentResponseInvalidError as exc:
                    failure_reason = exc.defect.value
                    continue
                except (AIProviderStateError, AIProviderContentError) as exc:
                    failure_reason = _provider_failure_reason(exc)
                    continue
                except TimeoutError:
                    failure_reason = _REVIEW_ATTEMPT_TIMEOUT_REASON
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
                return EvidenceRunCompleted(
                    answer_evidence=answer_evidence,
                    review_missing=reviewer_response.missing,
                )
        if failure_reason is None:
            # attemptは必ず1回以上回り各経路が理由を書くため、ここに来たら分類漏れ。
            raise RuntimeError("review exhausted attempts without a failure reason")
        return EvidenceRunFailed(failure_reason=failure_reason)


def _provider_failure_reason(
    exc: AIProviderStateError | AIProviderContentError,
) -> str:
    # forensics用途では詳細(reason)を優先し、無い場合だけ回復クラスのCODEに落とす。
    return exc.reason.value if exc.reason is not None else exc.CODE
