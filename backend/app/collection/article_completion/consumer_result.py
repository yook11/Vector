"""補完の確定結果として、成功・追加処理が不要な理由・失敗後の判断を伝える。"""

from dataclasses import dataclass
from typing import Literal

from app.collection.article_completion.consumer_failure_classification import (
    CloseArticleCompletion,
    RetryArticleCompletion,
)


@dataclass(frozen=True, slots=True)
class CompletionSucceeded:
    """完成記事の保存と未完成行の削除を確定した記事のIDを保持する。"""

    analyzable_article_id: int


@dataclass(frozen=True, slots=True)
class CompletionNotRequired:
    """今回の記事補完を進める必要がなくなった理由を保持する。"""

    reason: Literal["missing", "closed", "superseded", "url_conflict"]


@dataclass(frozen=True, slots=True)
class CompletionFailed:
    """補完の元例外と、状態の確定を反映した工程判断を保持する。"""

    error: Exception
    decision: RetryArticleCompletion | CloseArticleCompletion
