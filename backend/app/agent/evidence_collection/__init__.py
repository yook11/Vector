"""Evidence collection package。"""

from app.agent.evidence_collection.contract import (
    EvidenceCollectionOutcome,
    ResearchTaskReport,
)
from app.agent.evidence_collection.researcher import (
    ExternalCollectionStatus,
    Researcher,
    ResearchTaskCandidates,
)

__all__ = [
    "EvidenceCollectionOutcome",
    "ExternalCollectionStatus",
    "Researcher",
    "ResearchTaskCandidates",
    "ResearchTaskReport",
]
