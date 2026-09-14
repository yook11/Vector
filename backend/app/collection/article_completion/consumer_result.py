"""新経路の補完で追加処理が不要な理由と、失敗後の判断を伝える。"""

from dataclasses import dataclass
from typing import Literal

from app.collection.article_completion.consumer_failure_classification import (
    CloseArticleCompletion,
    RetryArticleCompletion,
)


@dataclass(frozen=True, slots=True)
class CompletionNotRequired:
    """今回の記事補完を進める必要がなくなった理由を保持する。"""

    reason: Literal["missing", "closed", "superseded", "url_conflict"]


@dataclass(frozen=True, slots=True)
class CompletionFailed:
    """補完の元例外と、状態の確定を反映した工程判断を保持する。"""

    error: Exception
    decision: RetryArticleCompletion | CloseArticleCompletion
