"""collection/extraction BC のドメイン層。

抽出された記事 Entity (``Article``) と公開日時 VO (``PublishedAt``) を
公開する。``ArticleDraft`` は永続化前のドメイン入力で、Repository / Service
経由の利用に限定するため、ここでは re-export しない (fully-qualified import
を強制)。
"""

from app.collection.extraction.domain.article import Article
from app.collection.extraction.domain.value_objects import PublishedAt

__all__ = ["Article", "PublishedAt"]
