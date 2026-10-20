"""backfillの抽出結果と再投入に必要な保存済み事実。"""

from dataclasses import dataclass
from datetime import datetime

from app.analysis.assessment.events import ArticleAssessedInScope
from app.analysis.curation.events import ArticleCuratedSignal
from app.collection.article_acquisition.events import IncompleteArticleRecorded
from app.collection.events import AnalyzableArticleCreated


@dataclass(frozen=True, slots=True)
class BackfillEventTarget:
    """保存済み事実の再投入に必要なpayloadと発生時刻。"""

    occurred_at: datetime
    payload: (
        AnalyzableArticleCreated
        | ArticleCuratedSignal
        | ArticleAssessedInScope
        | IncompleteArticleRecorded
    )
