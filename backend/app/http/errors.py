"""各工程へHTTPの発生事実を伝える共通エラー。"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from app.http.failure import HttpTransportFailure
from app.shared.errors import ApplicationError


class HttpError(ApplicationError):
    """HTTPの失敗を表し、工程ごとの対処は持たない。"""

    CODE: ClassVar[str] = "http_error"


class HttpTransportError(HttpError):
    """応答の受信を完了できなかった通信失敗を、段階と理由の共通の値で保持する。"""

    CODE: ClassVar[str] = "http_transport_error"

    def __init__(self, *, failure: HttpTransportFailure) -> None:
        super().__init__(
            "HTTP通信で応答を受け取れませんでした",
            details={
                "stage": failure.stage.value,
                "reason": failure.reason.value,
                "proxy_status": failure.proxy_status,
            },
        )
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
        # 生のRetry-Afterは取得先が任意に書ける値のため、診断情報へ入れない。
        super().__init__(
            "HTTPの非成功応答を受け取りました", details={"status_code": status_code}
        )
        self.status_code = status_code
        self.received_at = received_at
        self.retry_after = retry_after
