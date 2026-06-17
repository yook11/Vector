"""外部取得由来のエラー定義。

ニュースソース、RSS、HTML、sitemap など Vector 外部の取得境界で
起きうる失敗カテゴリを定義する。

本モジュールは「何が起きたか」という origin error の語彙を扱う。各 origin error は
``CODE`` (何が起きたか) と ``retryable`` (再実行で結果が変わりうるか=失敗の性質) を
持つ。403 は何度叩いても 403、timeout は変わりうる、という二値は段に依らない失敗
原因の本質なので origin 側に SSoT として持つ。

一方「いつ / どう retry するか (scheduling)」と「失敗時にどう後始末するか (action)」は
段ごとの事情なので各 Stage 側の marker / mapper が持つ。family は audit の
``Retryability`` enum を知らない (bool のみ公開し段境界を保つ)。
"""

from __future__ import annotations

from typing import ClassVar, Literal


class ExternalFetchError(Exception):
    """外部取得境界で発生した失敗の共通祖先。

    origin error の識別 marker。具体 subclass は ``CODE`` と ``retryable`` を
    override する。``retryable`` は「再実行で結果が変わりうるか」という失敗原因の
    性質であって handling ではない (scheduling / action は各 Stage 側)。

    ``__str__`` は明示 message があればそれを、空文字なら ``_default_message()``
    を返す (wrap 経路の監査 / ログが空文字にならないように)。
    """

    CODE: ClassVar[str]
    retryable: ClassVar[bool]

    def __str__(self) -> str:
        explicit = super().__str__()
        return explicit if explicit else self._default_message()

    def _default_message(self) -> str:
        """constructor message が空のときの合成既定 message。

        base は ``CODE`` のみ。``status_code`` / ``reason`` 等の origin metadata を
        持つ subclass は本 method を override してそれらを含める。
        """
        return self.CODE


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
    retryable: ClassVar[bool] = False

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

    def _default_message(self) -> str:
        return f"{self.CODE}: HTTP {self.status_code} ({self.reason})"


class FetchLegalBlockError(ExternalFetchError):
    """法的理由または公共政策上の理由により取得できなかった。

    代表例は HTTP 451。
    """

    CODE: ClassVar[str] = "fetch_legal_block"
    retryable: ClassVar[bool] = False

    def __init__(self, message: str = "", *, status_code: int = 451) -> None:
        super().__init__(message)
        self.status_code = status_code

    def _default_message(self) -> str:
        return f"{self.CODE}: HTTP {self.status_code}"


class FetchResourceNotFoundError(ExternalFetchError):
    """外部リソースが存在しない、または恒久的に削除されている。

    代表例は HTTP 404 / 410。``reason`` は not found / gone の観測差分を
    残すための origin metadata。
    """

    CODE: ClassVar[str] = "fetch_resource_not_found"
    retryable: ClassVar[bool] = False

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

    def _default_message(self) -> str:
        return f"{self.CODE}: HTTP {self.status_code} ({self.reason})"


class FetchRateLimitedError(ExternalFetchError):
    """外部サーバから rate limit を通知された。

    代表例は HTTP 429。``retry_after_seconds`` は ``Retry-After`` header 由来の
    origin metadata であり、実際に尊重するかどうかは Stage 側の policy が決める。
    """

    CODE: ClassVar[str] = "fetch_rate_limited"
    retryable: ClassVar[bool] = True

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

    def _default_message(self) -> str:
        if self.retry_after_seconds is not None:
            return (
                f"{self.CODE}: HTTP {self.status_code} "
                f"retry_after={self.retry_after_seconds}s"
            )
        return f"{self.CODE}: HTTP {self.status_code}"


class FetchOriginServerError(ExternalFetchError):
    """外部サーバ自身の 5xx 系失敗。

    代表例は HTTP 500 / 503。``reason`` は internal error / service unavailable
    の観測差分を残すための origin metadata。``retry_after_seconds`` は HTTP 503 の
    ``Retry-After`` header 由来の値。
    """

    CODE: ClassVar[str] = "fetch_origin_server_error"
    retryable: ClassVar[bool] = True

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

    def _default_message(self) -> str:
        base = f"{self.CODE}: HTTP {self.status_code} ({self.reason})"
        if self.retry_after_seconds is not None:
            return f"{base} retry_after={self.retry_after_seconds}s"
        return base


class FetchGatewayError(ExternalFetchError):
    """gateway / proxy 経路で外部取得に失敗した。

    代表例は HTTP 502 / 504。TCP 接続失敗や DNS 失敗などの transport failure は
    ``FetchNetworkError`` に分ける。
    """

    CODE: ClassVar[str] = "fetch_gateway_failure"
    retryable: ClassVar[bool] = True

    def __init__(self, message: str = "", *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code

    def _default_message(self) -> str:
        return f"{self.CODE}: HTTP {self.status_code}"


class FetchRequestTimeoutError(ExternalFetchError):
    """HTTP status として request timeout が返された。

    代表例は HTTP 408。transport 層で timeout exception が発生した場合は
    ``FetchTimeoutError`` に分ける。
    """

    CODE: ClassVar[str] = "fetch_request_timeout"
    retryable: ClassVar[bool] = True

    def __init__(self, message: str = "", *, status_code: int = 408) -> None:
        super().__init__(message)
        self.status_code = status_code

    def _default_message(self) -> str:
        return f"{self.CODE}: HTTP {self.status_code}"


class FetchRetryableStatusError(ExternalFetchError):
    """専用クラスを持たないが retry 前提で解釈できる HTTP status。

    代表例は HTTP 425 Too Early。Stage mapper が ``status_code`` を直接叩かずに
    retryable status として扱えるようにするための分類。
    """

    CODE: ClassVar[str] = "fetch_retryable_status"
    retryable: ClassVar[bool] = True

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

    def _default_message(self) -> str:
        return f"{self.CODE}: HTTP {self.status_code} ({self.reason})"


class FetchUnexpectedClientStatusError(ExternalFetchError):
    """明示分類しない 4xx、および 2xx/3xx/5xx いずれでもない分類不能 status。

    1xx や範囲外 (600/700 等) も再実行で結果が変わらない terminal としてここへ倒す。
    distinct な扱いを要する status が出るまでの保守的な escape hatch。
    """

    CODE: ClassVar[str] = "fetch_unexpected_client_status"
    retryable: ClassVar[bool] = False

    def __init__(self, message: str = "", *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code

    def _default_message(self) -> str:
        return f"{self.CODE}: HTTP {self.status_code}"


class FetchUnexpectedServerStatusError(ExternalFetchError):
    """明示分類しない 5xx による取得失敗。

    server 起因の一時障害として retry 前提で扱う escape hatch。
    """

    CODE: ClassVar[str] = "fetch_unexpected_server_status"
    retryable: ClassVar[bool] = True

    def __init__(self, message: str = "", *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code

    def _default_message(self) -> str:
        return f"{self.CODE}: HTTP {self.status_code}"


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
    retryable: ClassVar[bool] = True


class FetchNetworkError(ExternalFetchError):
    """外部サーバとの通信経路で失敗した。

    代表例は DNS 解決失敗、TCP 接続失敗、TLS handshake 失敗、mid-stream disconnect、
    remote protocol violation など。HTTP status として返ってきた 502 / 504 は
    ``FetchGatewayError`` に分ける。
    """

    CODE: ClassVar[str] = "fetch_network"
    retryable: ClassVar[bool] = True


# ---------------------------------------------------------------------------
# Policy / security 起因
# ---------------------------------------------------------------------------


class FetchSsrfBlockedError(ExternalFetchError):
    """SSRF guard により外部取得を遮断した。

    代表例は private IP / link-local / loopback 宛の遮断。security 以外の
    Vector 側 policy で止める失敗が増えた場合は別 class を追加する。
    """

    CODE: ClassVar[str] = "fetch_ssrf_blocked"
    retryable: ClassVar[bool] = False


class FetchRobotsDisallowedError(ExternalFetchError):
    """robots.txt の明示的な Disallow により対象 URL の取得が許可されなかった。"""

    CODE: ClassVar[str] = "fetch_robots_disallowed"
    retryable: ClassVar[bool] = False


class FetchRobotsUnavailableError(ExternalFetchError):
    """robots.txt 自体を安定して取得できず、取得不可として扱った。

    RFC 9309 の complete disallow 相当の状態を表す。明示的な Disallow は
    ``FetchRobotsDisallowedError`` に分ける。
    """

    CODE: ClassVar[str] = "fetch_robots_unavailable"
    retryable: ClassVar[bool] = False


class FetchRedirectBlockedError(ExternalFetchError):
    """redirect を追従しない policy により取得を止めた。

    Location 経由の SSRF や意図しない外部遷移を避けるための拒否を表す。
    ``status_code`` は 3xx の観測値を残すための origin metadata。Location header は
    token を含みうるため保持しない。
    """

    CODE: ClassVar[str] = "fetch_redirect_blocked"
    retryable: ClassVar[bool] = False

    def __init__(self, message: str = "", *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code

    def _default_message(self) -> str:
        if self.status_code is not None:
            return f"{self.CODE}: HTTP {self.status_code}"
        return self.CODE


class FetchRedirectLoopError(ExternalFetchError):
    """redirect loop または redirect 回数超過により取得できなかった。

    redirect を最初から追従しない policy は ``FetchRedirectBlockedError`` に分ける。
    """

    CODE: ClassVar[str] = "fetch_redirect_loop"
    retryable: ClassVar[bool] = False

    def __init__(
        self,
        message: str = "",
        *,
        redirect_count: int | None = None,
    ) -> None:
        super().__init__(message)
        self.redirect_count = redirect_count

    def _default_message(self) -> str:
        if self.redirect_count is not None:
            return f"{self.CODE}: redirect_count={self.redirect_count}"
        return self.CODE


# ---------------------------------------------------------------------------
# Payload / content 起因
# ---------------------------------------------------------------------------


class FetchResponseTooLargeError(ExternalFetchError):
    """外部レスポンスが許容サイズを超過した。"""

    CODE: ClassVar[str] = "fetch_response_too_large"
    retryable: ClassVar[bool] = False

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

    def _default_message(self) -> str:
        if self.actual_bytes is not None and self.limit_bytes is not None:
            return f"{self.CODE}: actual={self.actual_bytes}B limit={self.limit_bytes}B"
        return self.CODE


class FetchContentTypeMismatchError(ExternalFetchError):
    """取得できた content type が期待する形式ではなかった。

    代表例は HTML を期待した経路で ``Content-Type`` が ``text/html`` ではない場合。
    """

    CODE: ClassVar[str] = "fetch_content_type_mismatch"
    retryable: ClassVar[bool] = False

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

    def _default_message(self) -> str:
        return (
            f"{self.CODE}: expected={self.expected_content_type} "
            f"detected={self.detected_content_type}"
        )
