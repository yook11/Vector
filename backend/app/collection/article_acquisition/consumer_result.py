"""取得の成功または再配信の要否を、取得不要の理由と元例外を保って伝える。"""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class AcquisitionSucceeded:
    """取得を最後まで実行し、新しく保存した記事の件数を保持する。"""

    created_count: int


@dataclass(frozen=True, slots=True)
class RetryAcquisition:
    """SQSの再配信に任せる取得の失敗を保持する。"""

    error: Exception


@dataclass(frozen=True, slots=True)
class NoRetryAcquisition:
    """受信完了にできる取得不要の理由または再試行しない失敗を保持する。"""

    cause: Literal["inactive", "missing"] | Exception
