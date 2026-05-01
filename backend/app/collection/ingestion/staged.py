"""``extract_html_body`` task の kiq 引数 ``StagedArticle``。

PR-1b' (collection-acquisition-redesign Phase 1)。Pattern H 1 段目で
yield された ``PendingHtmlFetch`` を ``discovered_articles`` 行 ID と
束ねて 2 段目 task に渡す BaseModel。

taskiq は kiq 引数に Pydantic ``BaseModel(frozen=True)`` を要求する
(memory ``feedback_taskiq_basemodel_required.md``)。素の ``dataclass`` は
serializer が ``PydanticSerializationError`` で死ぬ。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.collection.ingestion.domain.fetched_article import PendingHtmlFetch


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
