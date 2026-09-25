"""各工程へHTTPの発生事実を伝える共通エラー。"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from app.http.failure import HttpTransportFailure


class HttpError(Exception):
    """HTTPの失敗を表し、工程ごとの対処は持たない。"""

    CODE: ClassVar[str] = "http_error"


class HttpTransportError(HttpError):
    """応答の受信を完了できなかった通信失敗を、段階と理由の共通の値で保持する。"""

    CODE: ClassVar[str] = "http_transport_error"

    def __init__(self, *, failure: HttpTransportFailure) -> None:
        super().__init__()
        self.failure = failure


class HttpResponseError(HttpError):
    """非成功応答の事実を保持し、応答の生成元やRetry-Afterの採用は判断しない。"""

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
