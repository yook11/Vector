"""Evidence collection package。"""

from app.agent.evidence_collection.contract import (
    CollectedNews,
    CollectedTask,
    ResearchTaskReport,
)
from app.agent.evidence_collection.news_collector import NewsCollector
from app.agent.evidence_collection.researcher import (
    ExternalCollectionStatus,
    Researcher,
    ResearchTaskCandidates,
)

__all__ = [
    "CollectedNews",
    "CollectedTask",
    "ExternalCollectionStatus",
    "NewsCollector",
    "Researcher",
    "ResearchTaskCandidates",
    "ResearchTaskReport",
]
