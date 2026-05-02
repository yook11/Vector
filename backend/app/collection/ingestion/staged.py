"""ingestion task 群の kiq 引数 envelope を集約。

taskiq は kiq 引数に Pydantic ``BaseModel(frozen=True)`` を要求する
(memory ``feedback_taskiq_basemodel_required.md``)。素の ``dataclass`` は
serializer が ``PydanticSerializationError`` で死ぬ。

- ``IngestSourceArg``: ``dispatch_sources`` → ``ingest_source`` 間 envelope。
  ``id`` (FK 用) と ``name`` (FETCHERS dispatch 用) を運び、Fetcher 側で
  ``NewsSource`` ORM を再 lookup する必要を消す。
- ``StagedArticle``: ``ingest_source`` → ``extract_html_body`` 間 envelope。
  Pattern H 1 段目で yield された ``PendingHtmlFetch`` を
  ``discovered_articles`` 行 ID と束ねて 2 段目 task に渡す。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.collection.ingestion.domain.fetched_article import PendingHtmlFetch


class IngestSourceArg(BaseModel):
    """``ingest_source`` task の kiq 引数 envelope。

    ``id``: ``news_sources.id`` (Article / DiscoveredArticle の FK で使う)。
    ``name``: ``news_sources.name`` (StrEnum 値)。``FETCHERS`` dispatch dict
    の lookup キー。

    ``dispatch_sources`` で 1 度だけ ``NewsSource`` を query して
    ``IngestSourceArg(id=..., name=...)`` を組み立て、kiq message に乗せる。
    これにより ``ingest_source`` task / ``IngestionService`` が DB を再
    lookup する必要が消え、Fetcher が ``NewsSource`` ORM を一切知らずに済む。
    """

    model_config = ConfigDict(frozen=True)

    id: int
    name: str


class StagedArticle(BaseModel):
    """``ingest_source`` → ``extract_html_body`` 間の Pydantic envelope。

    ``discovered_id``: ``ingest_source`` が作成した ``discovered_articles``
    行の ID。2 段目 task が ``articles.discovered_article_id`` の FK として
    使う (NOT NULL 制約を満たすため、新ルートでも discovered 行は必須)。

    ``pending``: RSS から救出された情報 (URL/title/published_at_hint/metadata)。
    """

    model_config = ConfigDict(frozen=True)

    discovered_id: int
    pending: PendingHtmlFetch
