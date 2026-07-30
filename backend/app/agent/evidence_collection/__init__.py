"""Evidence collection package。"""

from app.agent.evidence_collection.contract import (
    EvidenceCollectionOutcome,
    EvidenceReviewReport,
    ResearchTaskReport,
)
from app.agent.evidence_collection.researcher import (
    ExternalCollectionStatus,
    Researcher,
    ResearchTaskCandidates,
)

__all__ = [
    "EvidenceCollectionOutcome",
    "EvidenceReviewReport",
    "ExternalCollectionStatus",
    "Researcher",
    "ResearchTaskCandidates",
    "ResearchTaskReport",
]
