"""Run内の全taskの選択肢を1回で精査し、Run全体の根拠と不足を見極める
EvidenceReviewer。

provider clientの構築は composition が所有する scope factory に閉じ、Reviewer は
精査のあいだだけ Runtime を借りる(責任境界: Reviewer は client の作り方を知らない)。
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
from app.agent.evidence_review.failure import (
    EvidenceReviewError,
    evidence_review_error_from,
)
from app.agent.evidence_review.preparation import (
    EvidenceReviewInput,
    EvidenceReviewPreparation,
)
from app.agent.evidence_review.selection import EvidenceReviewerResponse
from app.agent.recording.evidence_review import (
    EvidenceReviewFailed,
    EvidenceReviewRecorder,
    EvidenceReviewSucceeded,
    logfire_evidence_review_recorder,
)
from app.agent.runtime.contract import (
    AgentResponseInvalidError,
    AgentRuntime,
    AgentRuntimeScopeFactory,
)
from app.analysis.ai_provider_errors import (
    AIProviderContentError,
    AIProviderStateError,
)

__all__ = ["EvidenceReviewer"]

_MAX_REVIEW_ATTEMPTS = 2
_REVIEW_ATTEMPT_TIMEOUT_SECONDS = 30
_REVIEW_SOURCE_ERRORS = (
    AgentResponseInvalidError,
    AIProviderStateError,
    AIProviderContentError,
    TimeoutError,
)


@dataclass(frozen=True, slots=True)
class EvidenceReviewer:
    """Run内の全taskの選択肢を1回の入力で精査する。収集と回答生成は持たない。"""

    runtime_scope_factory: AgentRuntimeScopeFactory
    recorder: EvidenceReviewRecorder = logfire_evidence_review_recorder

    async def review(
        self,
        *,
        tasks: list[CollectedTask],
        as_of: datetime,
    ) -> EvidenceRunResult:
        attempt_count = 0
        async with self.recorder.record(
            agent_name=EVIDENCE_REVIEWER_AGENT.name
        ) as recording:
            preparation = EvidenceReviewPreparation.from_tasks(tasks)
            review_input = EvidenceReviewInput(
                task_groups=preparation.task_groups,
                as_of=as_of,
            )
            last_error: EvidenceReviewError | None = None
            completed_result: EvidenceRunCompleted | None = None
            async with self.runtime_scope_factory() as reviewer_runtime:
                for attempt_number in range(1, _MAX_REVIEW_ATTEMPTS + 1):
                    attempt_count = attempt_number
                    try:
                        reviewer_response = await _review_attempt(
                            reviewer_runtime=reviewer_runtime,
                            review_input=review_input,
                            attempt_number=attempt_number,
                        )
                    except EvidenceReviewError as error:
                        last_error = error
                        continue

                    answer_evidence = AnswerEvidence.from_reviewer_response(
                        preparation=preparation,
                        reviewer_response=reviewer_response,
                    )
                    completed_result = EvidenceRunCompleted(
                        answer_evidence=answer_evidence,
                        review_missing=reviewer_response.missing,
                    )
                    break

            if completed_result is not None:
                recording.set_outcome(
                    EvidenceReviewSucceeded(attempt_count=attempt_count)
                )
                return completed_result
            if last_error is None:
                # attemptは必ず1回以上回り各経路が理由を書くため、ここに来たら分類漏れ。
                raise RuntimeError(
                    "review exhausted attempts without a classified error"
                )
            failed_result = EvidenceRunFailed(failure_code=last_error.code)
            recording.set_outcome(
                EvidenceReviewFailed(
                    failure_code=failed_result.failure_code,
                    attempt_count=attempt_count,
                )
            )
            return failed_result


async def _review_attempt(
    *,
    reviewer_runtime: AgentRuntime,
    review_input: EvidenceReviewInput,
    attempt_number: int,
) -> EvidenceReviewerResponse:
    try:
        draft = await asyncio.wait_for(
            reviewer_runtime.call(
                EVIDENCE_REVIEWER_AGENT,
                review_input,
                attempt_number=attempt_number,
            ),
            timeout=_REVIEW_ATTEMPT_TIMEOUT_SECONDS,
        )
    except _REVIEW_SOURCE_ERRORS as cause:
        raise evidence_review_error_from(cause) from cause

    try:
        return EvidenceReviewerResponse.from_draft(draft)
    except ValidationError as cause:
        raise evidence_review_error_from(cause) from cause
