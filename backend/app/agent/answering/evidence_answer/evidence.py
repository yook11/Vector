"""Evidence answer input normalization."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.agent.contract import AnswerSource, ExternalUrlSource, InternalArticleSource
from app.agent.evidence_collection import EvidenceCollectionOutcome
from app.agent.external_search import ExternalSearchEvidence
from app.agent.internal_retrieval.article_search import InternalArticleSearchHit

__all__ = ["AnswerEvidenceItem", "normalize_answer_evidence"]


class AnswerEvidenceItem(BaseModel):
    """回答向け本文と provenance 正本を対で持つ根拠 1 件。"""

    model_config = ConfigDict(frozen=True)

    source: AnswerSource
    text: str = Field(min_length=1)


def normalize_answer_evidence(
    outcome: EvidenceCollectionOutcome,
) -> list[AnswerEvidenceItem]:
    items: list[AnswerEvidenceItem] = []
    next_ref = 1

    for hit in outcome.internal_hits:
        items.append(_normalize_internal_hit(hit, source_ref=str(next_ref)))
        next_ref += 1

    if outcome.external_search is not None:
        for evidence in outcome.external_search.evidence:
            items.append(
                _normalize_external_evidence(evidence, source_ref=str(next_ref))
            )
            next_ref += 1

    return items


def _normalize_internal_hit(
    hit: InternalArticleSearchHit,
    *,
    source_ref: str,
) -> AnswerEvidenceItem:
    return AnswerEvidenceItem(
        source=InternalArticleSource(
            source_ref=source_ref,
            # Internal transport calls the public /news id assessment_id.
            article_id=hit.assessment_id,
            title=hit.content.title,
            published_at=hit.content.published_at,
        ),
        text=_internal_text(hit),
    )


def _normalize_external_evidence(
    evidence: ExternalSearchEvidence,
    *,
    source_ref: str,
) -> AnswerEvidenceItem:
    return AnswerEvidenceItem(
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


def _internal_text(hit: InternalArticleSearchHit) -> str:
    if not hit.content.key_points:
        return hit.content.summary
    key_points = "\n".join(f"- {point}" for point in hit.content.key_points)
    return f"{hit.content.summary}\n{key_points}"


def _external_text(evidence: ExternalSearchEvidence) -> str:
    if not evidence.snippet:
        return evidence.claim
    return f"{evidence.claim}\n{evidence.snippet}"
