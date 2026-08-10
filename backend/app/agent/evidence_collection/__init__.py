"""Evidence collection package。"""

from app.agent.evidence_collection.contract import (
    CollectedNews,
    CollectedTask,
    EvidenceCollectionOutcome,
    EvidenceReviewReport,
    ResearchTaskReport,
    ReviewedEvidence,
)
from app.agent.evidence_collection.news_collector import NewsCollector
from app.agent.evidence_collection.researcher import (
    ExternalCollectionStatus,
    Researcher,
    ResearchTaskCandidates,
)
from app.agent.evidence_collection.run_review import review_collected_news

__all__ = [
    "CollectedNews",
    "CollectedTask",
    "EvidenceCollectionOutcome",
    "EvidenceReviewReport",
    "ExternalCollectionStatus",
    "NewsCollector",
    "Researcher",
    "ResearchTaskCandidates",
    "ResearchTaskReport",
    "ReviewedEvidence",
    "review_collected_news",
]
