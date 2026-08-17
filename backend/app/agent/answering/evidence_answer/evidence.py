"""AnswerEvidenceをAnswerer入力へ投影する。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.agent.contract import AnswerSource, ExternalUrlSource, InternalArticleSource
from app.agent.evidence_review import (
    AnswerEvidence,
    ExternalSearchEvidence,
    InternalArticleEvidence,
)

__all__ = ["AnswerInputEvidence", "build_answer_input_evidence"]


class AnswerInputEvidence(BaseModel):
    """Answererへ渡す本文とprovenanceを対で持つ入力根拠1件。"""

    model_config = ConfigDict(frozen=True)

    source: AnswerSource
    text: str = Field(min_length=1)


def build_answer_input_evidence(
    evidence: AnswerEvidence,
) -> list[AnswerInputEvidence]:
    """確定根拠を、回答内citation用の連番を持つ入力projectionへ変換する。"""
    items: list[AnswerInputEvidence] = []
    next_ref = 1

    for item in evidence.internal_evidence:
        items.append(_normalize_internal_evidence(item, source_ref=str(next_ref)))
        next_ref += 1

    for item in evidence.external_evidence:
        items.append(_normalize_external_evidence(item, source_ref=str(next_ref)))
        next_ref += 1

    return items


def _normalize_internal_evidence(
    evidence: InternalArticleEvidence,
    *,
    source_ref: str,
) -> AnswerInputEvidence:
    return AnswerInputEvidence(
        source=InternalArticleSource(
            source_ref=source_ref,
            # Internal transport calls the public /news id assessment_id.
            article_id=evidence.assessment_id,
            title=evidence.title,
            published_at=evidence.published_at,
        ),
        text=_internal_text(evidence),
    )


def _normalize_external_evidence(
    evidence: ExternalSearchEvidence,
    *,
    source_ref: str,
) -> AnswerInputEvidence:
    return AnswerInputEvidence(
        source=ExternalUrlSource(
            source_ref=source_ref,
            url=evidence.url,
            title=evidence.title,
            evidence_claim=evidence.claim,
            published_at=evidence.published_at,
            source_name=evidence.source_name,
        ),
        text=_external_text(evidence),
    )


def _internal_text(evidence: InternalArticleEvidence) -> str:
    if not evidence.key_points:
        return f"{evidence.claim}\n{evidence.summary}"
    key_points = "\n".join(f"- {point}" for point in evidence.key_points)
    return f"{evidence.claim}\n{evidence.summary}\n{key_points}"


def _external_text(evidence: ExternalSearchEvidence) -> str:
    if not evidence.snippet:
        return evidence.claim
    return f"{evidence.claim}\n{evidence.snippet}"
