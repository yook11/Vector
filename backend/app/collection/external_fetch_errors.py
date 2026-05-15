"""外部取得由来のエラー定義。

ニュースソース、RSS、HTML、sitemap など Vector 外部の取得境界で
起きうる失敗カテゴリを定義する。

本モジュールは「何が起きたか」という origin error の語彙を扱う。
Stage 1 / Stage 2 でどうハンドリングするかは、各 Stage 側の marker
または mapper で表現する。
"""

from __future__ import annotations

from typing import ClassVar, Literal


class ExternalFetchError(Exception):
    """外部取得境界で発生した失敗の共通祖先。

    本クラスは origin error の識別 marker。Stage 1 / Stage 2 の retry 判断や
    terminal 判断はここでは持たず、各 Stage 側の mapper / marker で解釈する。
    具体 subclass は ``CODE`` を必ず override する。
    """

    CODE: ClassVar[str]


AccessDeniedReason = Literal["unauthorized", "forbidden"]
ResourceMissingReason = Literal["not_found", "gone"]
OriginServerErrorReason = Literal["internal_error", "service_unavailable"]
RetryableStatusReason = Literal["too_early", "other"]


# ---------------------------------------------------------------------------
# HTTP status 起因
# ---------------------------------------------------------------------------


class FetchAccessDeniedError(ExternalFetchError):
    """外部リソースへのアクセスが拒否された。

    代表例は HTTP 401 / 403。HTTP 451 は法的ブロックとして
    ``FetchLegalBlockError`` に分ける。robots.txt 拒否は crawl policy 起因として
    ``FetchRobotsDisallowedError`` に分ける。``reason`` は 401 / 403 の観測差分を
    残すための origin metadata。
    """

    CODE: ClassVar[str] = "fetch_access_denied"

    def __init__(
        self,
        message: str = "",
        *,
        status_code: int,
        reason: AccessDeniedReason,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason


class FetchLegalBlockError(ExternalFetchError):
    """法的理由または公共政策上の理由により取得できなかった。

    代表例は HTTP 451。
    """

    CODE: ClassVar[str] = "fetch_legal_block"

    def __init__(self, message: str = "", *, status_code: int = 451) -> None:
        super().__init__(message)
        self.status_code = status_code


class FetchResourceNotFoundError(ExternalFetchError):
    """外部リソースが存在しない、または恒久的に削除されている。

    代表例は HTTP 404 / 410。``reason`` は not found / gone の観測差分を
    残すための origin metadata。
    """

    CODE: ClassVar[str] = "fetch_resource_not_found"

    def __init__(
        self,
        message: str = "",
        *,
        status_code: int,
        reason: ResourceMissingReason,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason


class FetchRateLimitedError(ExternalFetchError):
    """外部サーバから rate limit を通知された。

    代表例は HTTP 429。``retry_after_seconds`` は ``Retry-After`` header 由来の
    origin metadata であり、実際に尊重するかどうかは Stage 側の policy が決める。
    """

    CODE: ClassVar[str] = "fetch_rate_limited"

    def __init__(
        self,
        message: str = "",
        *,
        status_code: int = 429,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


class FetchOriginServerError(ExternalFetchError):
    """外部サーバ自身の 5xx 系失敗。

    代表例は HTTP 500 / 503。``reason`` は internal error / service unavailable
    の観測差分を残すための origin metadata。``retry_after_seconds`` は HTTP 503 の
    ``Retry-After`` header 由来の値。
    """

    CODE: ClassVar[str] = "fetch_origin_server_error"

    def __init__(
        self,
        message: str = "",
        *,
        status_code: int,
        reason: OriginServerErrorReason,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds


class FetchGatewayError(ExternalFetchError):
    """gateway / proxy 経路で外部取得に失敗した。

    代表例は HTTP 502 / 504。TCP 接続失敗や DNS 失敗などの transport failure は
    ``FetchNetworkError`` に分ける。
    """

    CODE: ClassVar[str] = "fetch_gateway_failure"

    def __init__(self, message: str = "", *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class FetchRequestTimeoutError(ExternalFetchError):
    """HTTP status として request timeout が返された。

    代表例は HTTP 408。transport 層で timeout exception が発生した場合は
    ``FetchTimeoutError`` に分ける。
    """

    CODE: ClassVar[str] = "fetch_request_timeout"

    def __init__(self, message: str = "", *, status_code: int = 408) -> None:
        super().__init__(message)
        self.status_code = status_code


class FetchRetryableStatusError(ExternalFetchError):
    """専用クラスを持たないが retry 前提で解釈できる HTTP status。

    代表例は HTTP 425 Too Early。Stage mapper が ``status_code`` を直接叩かずに
    retryable status として扱えるようにするための分類。
    """

    CODE: ClassVar[str] = "fetch_retryable_status"

    def __init__(
        self,
        message: str = "",
        *,
        status_code: int,
        reason: RetryableStatusReason = "other",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason


class FetchUnexpectedStatusError(ExternalFetchError):
    """明示分類していない HTTP status による取得失敗。

    新しい status 判定を追加するまでの保守的な escape hatch。
    """

    CODE: ClassVar[str] = "fetch_unexpected_status"

    def __init__(self, message: str = "", *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Transport 起因
# ---------------------------------------------------------------------------


class FetchTimeoutError(ExternalFetchError):
    """外部取得が timeout した。

    connect timeout / read timeout など、HTTP response を安定して得る前後の
    timeout exception を表す。HTTP status として返った 408 は
    ``FetchRequestTimeoutError`` に分ける。timeout 種別を細分化する必要が出たら
    subclass を追加する。
    """

    CODE: ClassVar[str] = "fetch_timeout"


class FetchNetworkError(ExternalFetchError):
    """外部サーバとの通信経路で失敗した。

    代表例は DNS 解決失敗、TCP 接続失敗、TLS handshake 失敗、mid-stream disconnect、
    remote protocol violation など。HTTP status として返ってきた 502 / 504 は
    ``FetchGatewayError`` に分ける。
    """

    CODE: ClassVar[str] = "fetch_network"


# ---------------------------------------------------------------------------
# Policy / security 起因
# ---------------------------------------------------------------------------


class FetchSsrfBlockedError(ExternalFetchError):
    """SSRF guard により外部取得を遮断した。

    代表例は private IP / link-local / loopback 宛の遮断。security 以外の
    Vector 側 policy で止める失敗が増えた場合は別 class を追加する。
    """

    CODE: ClassVar[str] = "fetch_ssrf_blocked"


class FetchRobotsDisallowedError(ExternalFetchError):
    """robots.txt の明示的な Disallow により対象 URL の取得が許可されなかった。"""

    CODE: ClassVar[str] = "fetch_robots_disallowed"


class FetchRobotsUnavailableError(ExternalFetchError):
    """robots.txt 自体を安定して取得できず、取得不可として扱った。

    RFC 9309 の complete disallow 相当の状態を表す。明示的な Disallow は
    ``FetchRobotsDisallowedError`` に分ける。
    """

    CODE: ClassVar[str] = "fetch_robots_unavailable"


class FetchRedirectBlockedError(ExternalFetchError):
    """redirect を追従しない policy により取得を止めた。

    Location 経由の SSRF や意図しない外部遷移を避けるための拒否を表す。
    """

    CODE: ClassVar[str] = "fetch_redirect_blocked"


class FetchRedirectLoopError(ExternalFetchError):
    """redirect loop または redirect 回数超過により取得できなかった。

    redirect を最初から追従しない policy は ``FetchRedirectBlockedError`` に分ける。
    """

    CODE: ClassVar[str] = "fetch_redirect_loop"

    def __init__(
        self,
        message: str = "",
        *,
        redirect_count: int | None = None,
    ) -> None:
        super().__init__(message)
        self.redirect_count = redirect_count


# ---------------------------------------------------------------------------
# Payload / content 起因
# ---------------------------------------------------------------------------


class FetchResponseTooLargeError(ExternalFetchError):
    """外部レスポンスが許容サイズを超過した。"""

    CODE: ClassVar[str] = "fetch_response_too_large"

    def __init__(
        self,
        message: str = "",
        *,
        actual_bytes: int | None = None,
        limit_bytes: int | None = None,
    ) -> None:
        super().__init__(message)
        self.actual_bytes = actual_bytes
        self.limit_bytes = limit_bytes


class FetchContentTypeMismatchError(ExternalFetchError):
    """取得できた content type が期待する形式ではなかった。

    代表例は HTML を期待した経路で ``Content-Type`` が ``text/html`` ではない場合。
    """

    CODE: ClassVar[str] = "fetch_content_type_mismatch"

    def __init__(
        self,
        message: str = "",
        *,
        expected_content_type: str,
        detected_content_type: str | None,
    ) -> None:
        super().__init__(message)
        self.expected_content_type = expected_content_type
        self.detected_content_type = detected_content_type


class FetchParseError(ExternalFetchError):
    """取得した payload の parse に失敗した。

    RSS / Atom / sitemap / HTML など、取得後の外部 payload 解釈に失敗したことを
    表す。HTTP response を受け取った後の content decoding 失敗もここに含める。
    記事品質ゲートの不合格は domain validation として別軸で扱う。
    """

    CODE: ClassVar[str] = "fetch_payload_parse_failed"
