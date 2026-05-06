"""Collection 層の共通ドメイン例外。

ingestion / extraction の両方で、外部リソース取得の失敗を
リトライ可否の観点で分類する例外。

PR2.5-B: ``TemporaryFetchError`` をエラー特性 (blip / outage / timeout /
server-instructed) で細分化し、``retry_policy_for(exc)`` が exception 階層
だけ見れば policy を一意に決められるようにした。階層は extractor 側で
HTTP status / httpx exception 種別から分岐して raise する。

| サブクラス | 由来 | 復旧時間 | policy |
|---|---|---|---|
| ``ServerErrorBlip`` | 502/504/ConnectError | 秒〜1 分 | blip 系、密に retry |
| ``ServerErrorOutage`` | 500/503 (no Retry-After) | 数十分 | outage 系、長尺粘り |
| ``ServerErrorRetryAfter`` | 503 with Retry-After | サーバ指示 | header 値尊重 |
| ``ReadTimeout`` | httpx.ReadTimeout 系 | サーバ負荷次第 | blip 寄り |

429 (rate limit) は別軸 (per-source rate limit) のため本 PR では扱わない。
未分類の 5xx / RequestError は素の ``TemporaryFetchError`` のままにし、
``retry_policy_for`` が UNKNOWN_POLICY (保守的な outage 寄り) を返す。
"""

from __future__ import annotations


class PermanentFetchError(Exception):
    """リトライ不可のフェッチ失敗（403 / 404 / 410 / 451 / robots.txt 拒否）。"""


class TemporaryFetchError(Exception):
    """リトライ可能なフェッチ失敗の基底。

    生で raise される場合は「分類不能な temporary」として
    ``UNKNOWN_POLICY`` (保守的な outage 寄り) で扱う。
    """


class ServerErrorBlip(TemporaryFetchError):
    """gateway blip 系: 502 / 504 / ConnectError / DNS の瞬間失敗。

    秒〜1 分で復旧する典型的なエラー、密に retry する policy 対象。
    """


class ServerErrorOutage(TemporaryFetchError):
    """outage 系: 500 / 503 (no Retry-After) などの長尺障害。

    数十分〜数時間の outage を想定、長尺の delay schedule で粘る。
    """


class ServerErrorRetryAfter(TemporaryFetchError):
    """サーバが ``Retry-After`` header で再試行時刻を指示した失敗。

    Vector policy より server 指示を優先する。
    ``retry_after_seconds`` は当該値、後続 cap は呼び出し側で適用する。
    """

    def __init__(self, message: str, *, retry_after_seconds: float) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class ReadTimeout(TemporaryFetchError):
    """HTTP read timeout (httpx.ReadTimeout / TimeoutException 系)。

    blip 寄りだが timeout は構造的に長めの delay にする方が実用的、
    blip と outage の中間 policy で扱う。
    """
