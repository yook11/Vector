"""backfillの抽出結果と再投入に必要な保存済み事実。"""

from dataclasses import dataclass
from datetime import datetime

from app.analysis.assessment.events import ArticleAssessedInScope
from app.analysis.curation.events import ArticleCuratedSignal
from app.collection.events import AnalyzableArticleCreated
from app.collection.sources.source_name import SourceName


@dataclass(frozen=True, slots=True)
class BackfillTarget:
    """backfill が enqueue と監査に使う対象 snapshot。"""

    target_id: int
    analyzable_article_id: int
    source_name: SourceName | None


@dataclass(frozen=True, slots=True)
class BackfillEventTarget:
    """保存済み事実の再投入に必要なpayload・発生時刻・監査主語。"""

    target: BackfillTarget
    occurred_at: datetime
    payload: AnalyzableArticleCreated | ArticleCuratedSignal | ArticleAssessedInScope
