"""Evidence collection package。"""

from app.agent.evidence_collection.contract import (
    CollectedNews,
    CollectedTask,
    EvidenceCollector,
    ResearchTaskReport,
)
from app.agent.evidence_collection.news_collector import NewsCollector
from app.agent.evidence_collection.researcher import (
    ExternalCollectionStatus,
    Researcher,
    ResearchTaskHits,
)

__all__ = [
    "CollectedNews",
    "CollectedTask",
    "EvidenceCollector",
    "ExternalCollectionStatus",
    "NewsCollector",
    "Researcher",
    "ResearchTaskHits",
    "ResearchTaskReport",
]
