"""Evidence collection package。"""

from app.agent.evidence_collection.contract import (
    CollectedNews,
    CollectedTask,
    EvidenceCollector,
    ResearchTaskReport,
)
from app.agent.evidence_collection.service import EvidenceCollectionService

__all__ = [
    "CollectedNews",
    "CollectedTask",
    "EvidenceCollector",
    "EvidenceCollectionService",
    "ResearchTaskReport",
]
