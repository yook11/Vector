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
_REVIEW_TIMEOUT_SECONDS = 15
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
        async with self.recorder.record(
            agent_name=EVIDENCE_REVIEWER_AGENT.name
        ) as recording:
            preparation = EvidenceReviewPreparation.from_tasks(tasks)
            review_input = EvidenceReviewInput(
                task_groups=preparation.task_groups,
                as_of=as_of,
            )
            attempt_number = 0
            timeout = asyncio.timeout(_REVIEW_TIMEOUT_SECONDS)
            try:
                async with timeout:
                    async with self.runtime_scope_factory() as reviewer_runtime:
                        for attempt_number in range(1, _MAX_REVIEW_ATTEMPTS + 1):
                            try:
                                reviewer_response = await _review_attempt(
                                    reviewer_runtime=reviewer_runtime,
                                    review_input=review_input,
                                    attempt_number=attempt_number,
                                )
                                result = EvidenceRunCompleted(
                                    answer_evidence=AnswerEvidence.from_reviewer_response(
                                        preparation=preparation,
                                        reviewer_response=reviewer_response,
                                    ),
                                    review_missing=reviewer_response.missing,
                                )
                            except EvidenceReviewError:
                                if attempt_number < _MAX_REVIEW_ATTEMPTS:
                                    continue
                                raise
                            break
                        else:
                            raise AssertionError(
                                "unreachable: review loop must return or raise"
                            )
            except TimeoutError as cause:
                if not timeout.expired():
                    raise
                error = evidence_review_error_from(cause)
                recording.report_outcome(
                    EvidenceReviewFailed(
                        failure_code=error.code,
                        attempt_count=attempt_number,
                    )
                )
                return EvidenceRunFailed(failure_code=error.code)
            except EvidenceReviewError as error:
                recording.report_outcome(
                    EvidenceReviewFailed(
                        failure_code=error.code,
                        attempt_count=attempt_number,
                    )
                )
                return EvidenceRunFailed(failure_code=error.code)

            recording.report_outcome(
                EvidenceReviewSucceeded(attempt_count=attempt_number)
            )
            return result


async def _review_attempt(
    *,
    reviewer_runtime: AgentRuntime,
    review_input: EvidenceReviewInput,
    attempt_number: int,
) -> EvidenceReviewerResponse:
    try:
        draft = await reviewer_runtime.call(
            EVIDENCE_REVIEWER_AGENT,
            review_input,
            attempt_number=attempt_number,
        )
    except _REVIEW_SOURCE_ERRORS as cause:
        raise evidence_review_error_from(cause) from cause

    try:
        return EvidenceReviewerResponse.from_draft(draft)
    except ValidationError as cause:
        raise evidence_review_error_from(cause) from cause
