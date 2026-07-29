"""1つの ResearchTask について、内部候補と外部候補を精査し根拠を見極める
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
)
from app.agent.evidence_collection.evidence_review.policy import (
    EVIDENCE_REVIEW_TIMEOUT_SECONDS,
    REVIEWER_TIMEOUT_REASON,
    build_review_candidate_projection,
    build_review_evidence,
    finalize_review_draft,
    resolve_reviewer_failure_reason,
)
from app.agent.evidence_collection.external_search.contract import (
    ExternalSearchCandidate,
)
from app.agent.evidence_collection.internal_search.contract import (
    InternalArticleSearchHit,
)
from app.agent.phase_span import agent_phase
from app.agent.planning.contract import ResearchTask
from app.agent.runtime.contract import (
    AgentResponseDefect,
    AgentResponseInvalidError,
    AgentRuntime,
)
from app.analysis.ai_provider_errors import AIProviderError

__all__ = ["EvidenceReviewer"]

_EVIDENCE_REVIEW_PHASE = "evidence_review"


@dataclass(frozen=True, slots=True)
class EvidenceReviewer:
    """1つのResearchTaskについて内部・外部候補を精査する。収集と回答生成は持たない。"""

    async def review(
        self,
        *,
        task_index: int,
        task: ResearchTask,
        content_requirements: tuple[str, ...],
        internal_hits: list[InternalArticleSearchHit],
        external_candidates: list[ExternalSearchCandidate],
        as_of: datetime,
        reviewer_runtime: AgentRuntime,
    ) -> EvidenceReviewOutcome:
        review_input = EvidenceReviewInput(
            research_goal=task.research_goal,
            candidates=build_review_candidate_projection(
                internal_hits=internal_hits,
                external_candidates=external_candidates,
            ),
            content_requirements=content_requirements,
            as_of=as_of,
        )
        failure_reason: str | None = None
        with agent_phase(
            phase=_EVIDENCE_REVIEW_PHASE,
            agent_name=EVIDENCE_REVIEWER_AGENT.name,
            task_index=task_index,
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
                    task_index=task_index,
                    internal_hits=internal_hits,
                    external_candidates=external_candidates,
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
