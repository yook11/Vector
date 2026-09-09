"""取得機構を必要としないソースの識別・運用・補完情報。"""

from typing import Protocol

from app.collection.domain.observed_article import ObservedOrigin
from app.collection.sources.article_completion_policy import ArticleCompletionPolicy
from app.collection.sources.fetch_cadence import FetchCadence
from app.collection.sources.source_name import SourceName


class SourceMetadata(Protocol):
    @property
    def name(self) -> SourceName: ...

    @property
    def observed_origin(self) -> ObservedOrigin: ...

    @property
    def completion_policy(self) -> ArticleCompletionPolicy: ...

    @property
    def fetch_cadence(self) -> FetchCadence: ...
