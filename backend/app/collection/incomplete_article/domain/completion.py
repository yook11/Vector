"""``IncompleteArticle.complete_with_html`` の失敗型。

Pattern H の HTML 補完で merge / invariant 違反が起きた場合の戻り値。
``pipeline_events.payload.reason_code`` には ``f"promotion_{...code}"`` 形式で
焼かれる。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

ArticleCompletionFailureCode = Literal["published_at_missing", "other"]
"""HTML 補完で起きる失敗種別。

- ``published_at_missing``: RSS hint / HTML フォールバックの両方で公開日時を取れず。
- ``other``: ``ReadyForArticle`` invariant 違反 (length / 形式) 等の派生失敗。
"""


class ArticleCompletionFailureReason(BaseModel):
    """補完失敗の理由。

    ``retryable`` は scheduler 再投入判定に使うが、現状の ``complete_with_html``
    は構造的失敗のみ返すため常に ``False`` 想定。
    ``detail`` は同 ``code`` の細分化 (``rss_and_html_both_missing`` 等)。
    """

    model_config = ConfigDict(frozen=True)

    code: ArticleCompletionFailureCode
    retryable: bool
    detail: str | None = None


class ArticleCompletionFailed(BaseModel):
    """``IncompleteArticle.complete_with_html`` の失敗戻り値。"""

    model_config = ConfigDict(frozen=True)

    reason: ArticleCompletionFailureReason
