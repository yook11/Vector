"""通信の失敗が再試行で解消しうるかを、起きたことの種類から返す。"""

from __future__ import annotations

from typing import assert_never

from app.http.failure import HttpTransportFailure, HttpTransportFailureReason


def is_resolvable_by_retry(failure: HttpTransportFailure) -> bool:
    """同じリクエストを送り直すだけで解消しうる通信の失敗かを返す。"""
    match failure.reason:
        case (
            HttpTransportFailureReason.TIMEOUT
            | HttpTransportFailureReason.DNS_RESOLUTION
            | HttpTransportFailureReason.TLS
            | HttpTransportFailureReason.NETWORK_IO
            | HttpTransportFailureReason.PROTOCOL_VIOLATION
        ):
            return True
        case HttpTransportFailureReason.PROXY:
            # プロキシ自身の拒否や認証の要求は、設定を直すまで変わらない。
            status = failure.proxy_status
            return status is None or status == 429 or 500 <= status <= 599
        # 証明書の誤りと分類できない失敗は、相手か環境を直すまで変わらない。
        case (
            HttpTransportFailureReason.CERTIFICATE_VERIFICATION
            | HttpTransportFailureReason.UNKNOWN
        ):
            return False
        case _:
            assert_never(failure.reason)
