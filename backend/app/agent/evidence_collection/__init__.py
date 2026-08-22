"""Evidence collection package。"""

from app.agent.evidence_collection.contract import (
    CollectedNews,
    CollectedTask,
    EvidenceCollector,
    ResearchTaskReport,
)
from app.agent.evidence_collection.service import EvidenceCollectionService
from app.agent.evidence_collection.task_collector import (
    ExternalCollectionStatus,
    ResearchTaskCollector,
    ResearchTaskHits,
)

__all__ = [
    "CollectedNews",
    "CollectedTask",
    "EvidenceCollector",
    "ExternalCollectionStatus",
    "EvidenceCollectionService",
    "ResearchTaskCollector",
    "ResearchTaskHits",
    "ResearchTaskReport",
]
