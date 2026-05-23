"""ソース取得失敗の例外階層。

Stage 1 / Stage 2 双方が raise する。Stage 1 は ``SourceFetchError`` で
まとめて catch する。Stage 2 は ``Permanent`` / ``Temporary*`` の subclass を
区別して retry policy を選ぶ (``article_completion/retry_policy.py``)。

429 (rate limit) は別軸 (per-source rate limit) のため本ファイルでは扱わない。
"""

from __future__ import annotations


class SourceFetchError(Exception):
    """ソース全体の取得に失敗した (共通基底)。

    Stage 1 はこの基底だけで catch する。Stage 2 は subclass を区別して
    retry policy を選ぶ。
    """


class PermanentFetchError(SourceFetchError):
    """リトライ不可のフェッチ失敗 (403 / 404 / 410 / 451 / robots.txt 拒否)。

    Stage 2 が ``incomplete_articles.status='closed'`` に閉じる判断に使う。
    """


class TemporaryFetchError(SourceFetchError):
    """リトライ可能なフェッチ失敗の基底。

    Stage 2 が subclass を区別して delay schedule を選ぶ。生で raise された場合は
    分類不能な temporary として保守的に扱われる。
    """


class ServerErrorBlip(TemporaryFetchError):
    """gateway blip 系: 502 / 504 / ConnectError / DNS の瞬間失敗 (秒〜1 分で復旧)。"""


class ServerErrorOutage(TemporaryFetchError):
    """outage 系: 500 / 503 (no Retry-After) などの長尺障害 (数十分〜数時間)。"""


class ServerErrorRetryAfter(TemporaryFetchError):
    """サーバが ``Retry-After`` header で再試行時刻を指示した失敗。

    ``retry_after_seconds`` は header 値。cap は Stage 2 側で適用する。
    """

    def __init__(self, message: str, *, retry_after_seconds: float) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class ReadTimeout(TemporaryFetchError):
    """HTTP read timeout (httpx.ReadTimeout / TimeoutException 系)。

    Stage 2 では blip と outage の中間 policy で扱う。
    """
