"""補完の成功または再試行の要否を、原因と待機時刻を保って伝える。"""

from dataclasses import dataclass
from typing import Literal

from app.collection.retry_at import RetryAt


@dataclass(frozen=True, slots=True)
class CompletionSucceeded:
    """完成記事の保存と未完成行の削除を確定した記事のIDを保持する。"""

    analyzable_article_id: int


@dataclass(frozen=True, slots=True)
class RetryCompletion:
    """再配信に任せる補完の失敗と、追加で待機が必要な時刻を保持する。"""

    error: Exception
    code: str
    retry_at: RetryAt | None = None
    """追加の待機指定がなければ通常の再配信に任せる。"""
    requires_investigation: bool = False


@dataclass(frozen=True, slots=True)
class NoRetryCompletion:
    """受信完了にできる補完不要の理由または終了を決めた例外を保持する。"""

    cause: Literal["missing", "closed", "superseded", "url_conflict"] | Exception
    code: str | None = None
    """失敗の分類コードを保持し、補完不要の場合はNoneとする。"""
    requires_investigation: bool = False
