"""``AnalyzableArticle`` — 分析工程に進める記事 (collection BC の出口契約)。

``id`` を持たない (identity は永続化後の関心事)。長さ境界の SSoT は
``article_limits``。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.collection.domain.article_limits import (
    ARTICLE_BODY_MAX_LENGTH,
    ARTICLE_BODY_MIN_LENGTH,
    ARTICLE_TITLE_MAX_LENGTH,
    ARTICLE_TITLE_MIN_LENGTH,
)
from app.collection.domain.value_objects import PublishedAt
from app.shared.value_objects.canonical_article_url import CanonicalArticleUrl


class AnalyzableArticle(BaseModel):
    """分析工程に進める記事。

    不変条件 (Field で保証):
    - ``title``: 1..500 文字
    - ``body``: 50..1_048_576 文字
    - ``published_at``: 必須
    - ``source_id`` / ``source_url``: 原産情報 (UNIQUE 衝突判定 / 監査に必須)
    - frozen: 生成後は不変
    """

    model_config = ConfigDict(frozen=True)

    title: str = Field(
        min_length=ARTICLE_TITLE_MIN_LENGTH, max_length=ARTICLE_TITLE_MAX_LENGTH
    )
    body: str = Field(
        min_length=ARTICLE_BODY_MIN_LENGTH, max_length=ARTICLE_BODY_MAX_LENGTH
    )
    published_at: PublishedAt
    source_id: int = Field(gt=0)
    source_url: CanonicalArticleUrl
