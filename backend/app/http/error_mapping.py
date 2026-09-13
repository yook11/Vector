"""新経路へHTTPの発生事実を渡し、対処と出力は呼び出し側に委ねる。"""

from datetime import datetime

import httpx

from app.http.errors import HttpResponseError, HttpTransportError
from app.http.failure import classify_httpx


def http_transport_error_from_exception(exc: Exception) -> HttpTransportError | None:
    """通信失敗だけを変換し、対象外は呼び出し側で元のまま伝播させる。"""
    failure = classify_httpx(exc)
    if failure is None:
        return None
    return HttpTransportError(failure=failure)


def http_response_error_from_exception(
    exc: httpx.HTTPStatusError,
    *,
    received_at: datetime,
) -> HttpResponseError:
    """応答の事実と呼び出し側が記録したUTC受信時刻を、解釈せず保持する。"""
    return HttpResponseError(
        status_code=exc.response.status_code,
        received_at=received_at,
        retry_after=exc.response.headers.get("Retry-After"),
    )
