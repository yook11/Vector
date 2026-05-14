"""Collection 層のドメイン例外。

Stage 1 (``ArticleAcquisitionService`` / ``ingest_source``) と Stage 2
(``ArticleCompletionService`` / ``extract_html_body``) の双方が raise する
ソース取得失敗の型階層を本ファイルに集約する。

Stage 1 と Stage 2 でハンドリング戦略が違うため、語彙の使い方も分けている:

- **Stage 1 (``ingest_source``)** — 救済戦略は cron 一本化 (次 tick 再 dispatch、
  taskiq inline retry を持たない)。``except SourceFetchError`` 1 本で catch。
- **Stage 2 (``article_completion``)** — 救済戦略は DB 駆動 retry
  (``pending_html_articles.ready_at`` + ``attempt_count``)。
  ``Permanent/TemporaryFetchError`` の subclass 軸で policy 計算。

Stage 1 は taskiq の秒単位 inline retry を持たない (``max_retries=0``) ため、
``PermanentFetchError`` と ``TemporaryFetchError`` の区別が業務的意味を持たない。
ソース全体が取れなかったという事実だけで監査して return し、30 分後の cron tick
で再 dispatch される。よって Stage 1 task 層は ``except SourceFetchError`` で
1 本にまとめて catch する。

Stage 2 は ``retry_policy_for(exc)`` で 4 サブクラス
(``ServerErrorBlip`` / ``ServerErrorOutage`` / ``ServerErrorRetryAfter`` /
``ReadTimeout``) を区別して delay schedule を選ぶため、細粒度な階層が業務軸として
意味を持つ (``article_completion/retry_policy.py`` 参照)。

| サブクラス | 由来 | 復旧時間 | Stage 2 policy |
|---|---|---|---|
| ``ServerErrorBlip`` | 502/504/ConnectError | 秒〜1 分 | blip 系、密に retry |
| ``ServerErrorOutage`` | 500/503 (no Retry-After) | 数十分 | outage 系、長尺粘り |
| ``ServerErrorRetryAfter`` | 503 with Retry-After | サーバ指示 | header 値尊重 |
| ``ReadTimeout`` | httpx.ReadTimeout 系 | サーバ負荷次第 | blip 寄り |

429 (rate limit) は別軸 (per-source rate limit) のため本ファイルでは扱わない。
未分類の 5xx / RequestError は素の ``TemporaryFetchError`` のままにし、Stage 2 の
``retry_policy_for`` が UNKNOWN_POLICY (保守的な outage 寄り) を返す。
"""

from __future__ import annotations


class SourceFetchError(Exception):
    """ソース全体の取得に失敗した (Collection BC 共通基底)。

    Stage 1 (``ingest_source``) はこの基底だけで catch し、taskiq inline retry を
    持たず監査して return する。次の cron tick で再 dispatch される設計のため、
    細分化された subclass を Stage 1 が区別する業務的意味はない。

    Stage 2 (``article_completion``) は subclass 軸 (``Permanent`` /
    ``Temporary*``) を区別して DB 駆動 retry policy を選ぶ。
    """


class PermanentFetchError(SourceFetchError):
    """リトライ不可のフェッチ失敗 (403 / 404 / 410 / 451 / robots.txt 拒否)。

    Stage 2 専用語彙: ``ArticleCompletionService`` が
    ``pending_html_articles.status='closed'`` (terminal) に閉じる判断に使う。
    Stage 1 は本クラスを直接参照せず ``SourceFetchError`` でまとめて catch する。
    """


class TemporaryFetchError(SourceFetchError):
    """リトライ可能なフェッチ失敗の基底。

    Stage 2 専用語彙: ``retry_policy_for`` で 4 サブクラスを区別し、delay schedule
    を選ぶ判定軸に使う。生で raise される場合は「分類不能な temporary」として
    ``UNKNOWN_POLICY`` (保守的な outage 寄り) で扱われる。Stage 1 は本クラスを
    直接参照せず ``SourceFetchError`` でまとめて catch する。
    """


class ServerErrorBlip(TemporaryFetchError):
    """gateway blip 系: 502 / 504 / ConnectError / DNS の瞬間失敗。

    秒〜1 分で復旧する典型的なエラー、Stage 2 で密に retry する policy 対象。
    """


class ServerErrorOutage(TemporaryFetchError):
    """outage 系: 500 / 503 (no Retry-After) などの長尺障害。

    数十分〜数時間の outage を想定、Stage 2 で長尺の delay schedule で粘る。
    """


class ServerErrorRetryAfter(TemporaryFetchError):
    """サーバが ``Retry-After`` header で再試行時刻を指示した失敗。

    Vector policy より server 指示を優先する。``retry_after_seconds`` は当該値、
    後続 cap は Stage 2 ``article_completion/retry_policy.py`` 側で適用する。
    """

    def __init__(self, message: str, *, retry_after_seconds: float) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class ReadTimeout(TemporaryFetchError):
    """HTTP read timeout (httpx.ReadTimeout / TimeoutException 系)。

    blip 寄りだが timeout は構造的に長めの delay にする方が実用的、Stage 2 では
    blip と outage の中間 policy で扱う。
    """
