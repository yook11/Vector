"""ingestion task 群の kiq 引数 envelope を集約。

taskiq は kiq 引数に Pydantic ``BaseModel(frozen=True)`` を要求する
(memory ``feedback_taskiq_basemodel_required.md``)。素の ``dataclass`` は
serializer が ``PydanticSerializationError`` で死ぬ。

- ``IngestSourceArg``: ``dispatch_sources`` → ``ingest_source`` 間 envelope。
  ``id`` (FK 用) と ``name`` (FETCHERS dispatch 用) を運び、Fetcher 側で
  ``NewsSource`` ORM を再 lookup する必要を消す。

PR2.5-B cutover で ``StagedArticle`` 経路は撤去された。Pattern H は
``pending_html_articles`` テーブル + cron poller (``dispatch_html_fetch_jobs``)
の DB 駆動に切り替わり、kiq envelope は不要になっている。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class IngestSourceArg(BaseModel):
    """``ingest_source`` task の kiq 引数 envelope。

    ``id``: ``news_sources.id`` (Article の FK で使う)。
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
