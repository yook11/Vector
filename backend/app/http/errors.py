"""各工程へHTTPの発生事実を伝える共通エラー。"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from app.http.failure import HttpTransportFailure
from app.logfire.exceptions import VectorDomainError


class HttpError(VectorDomainError):
    """HTTPの失敗を表し、工程ごとの対処は持たない。"""

    CODE: ClassVar[str] = "http_error"
    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("CODE",)


class HttpTransportError(HttpError):
    """通信失敗の種類と到達可能性を共通の値で保持する。"""

    CODE: ClassVar[str] = "http_transport_error"

    def __init__(self, *, failure: HttpTransportFailure) -> None:
        super().__init__()
        self.failure = failure


class HttpResponseError(HttpError):
    """相手の応答情報を保持し、Retry-Afterの採用や解釈は行わない。"""

    CODE: ClassVar[str] = "http_response_error"

    def __init__(
        self,
        *,
        status_code: int,
        received_at: datetime,
        retry_after: str | None = None,
    ) -> None:
        super().__init__()
        self.status_code = status_code
        self.received_at = received_at
        self.retry_after = retry_after
