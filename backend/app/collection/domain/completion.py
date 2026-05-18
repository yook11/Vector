"""HTML 補完の失敗型。

merge / invariant 違反が起きた場合の戻り値。失敗種別は
``ArticleCompletionFailureCode``。これをどう扱うかは ``article_completion`` の責務。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

ArticleCompletionFailureCode = Literal["published_at_missing", "ready_invariant_failed"]
"""HTML 補完で起きる失敗種別。

- ``published_at_missing``: RSS hint / HTML の両方で公開日時を取れず。
- ``ready_invariant_failed``: ``AnalyzableArticle`` の不変条件違反。
"""


class ArticleCompletionFailureReason(BaseModel):
    """補完失敗の理由。

    ``detail`` は同 ``code`` の細分化 (``rss_and_html_both_missing`` 等)。
    """

    model_config = ConfigDict(frozen=True)

    code: ArticleCompletionFailureCode
    detail: str | None = None


class ArticleCompletionFailed(BaseModel):
    """HTML 補完の失敗戻り値。"""

    model_config = ConfigDict(frozen=True)

    reason: ArticleCompletionFailureReason
