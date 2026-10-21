"""通信の失敗が再試行で解消しうるかの分類。通信せず、分類後の値を直接渡す。"""

from __future__ import annotations

import pytest

from app.http.failure import (
    HttpTransportFailure,
    HttpTransportFailureReason,
    HttpTransportStage,
)
from app.http.retry_prospect import is_resolvable_by_retry

S = HttpTransportStage
R = HttpTransportFailureReason


@pytest.mark.parametrize(
    "failure",
    [
        HttpTransportFailure(S.RECEIVE, R.TIMEOUT),
        HttpTransportFailure(S.CONNECT, R.DNS_RESOLUTION),
        HttpTransportFailure(S.CONNECT, R.TLS),
        HttpTransportFailure(S.RECEIVE, R.NETWORK_IO),
        HttpTransportFailure(S.RECEIVE, R.PROTOCOL_VIOLATION),
        HttpTransportFailure(S.CONNECT, R.PROXY),
        HttpTransportFailure(S.CONNECT, R.PROXY, proxy_status=429),
        HttpTransportFailure(S.CONNECT, R.PROXY, proxy_status=500),
        HttpTransportFailure(S.CONNECT, R.PROXY, proxy_status=599),
    ],
)
def test_interrupted_communication_is_resolvable(
    failure: HttpTransportFailure,
) -> None:
    """待ち切れ・名前解決・証明書以外のTLS・切断・プロトコル違反と、プロキシの混雑や障害は、送り直せば解消しうる失敗として分類する。"""
    assert is_resolvable_by_retry(failure) is True


@pytest.mark.parametrize(
    "failure",
    [
        HttpTransportFailure(S.CONNECT, R.CERTIFICATE_VERIFICATION),
        HttpTransportFailure(S.UNKNOWN, R.UNKNOWN),
        HttpTransportFailure(S.CONNECT, R.PROXY, proxy_status=403),
        HttpTransportFailure(S.CONNECT, R.PROXY, proxy_status=407),
        HttpTransportFailure(S.CONNECT, R.PROXY, proxy_status=499),
        HttpTransportFailure(S.CONNECT, R.PROXY, proxy_status=600),
    ],
)
def test_certificate_unclassified_and_proxy_refusal_are_not_resolvable(
    failure: HttpTransportFailure,
) -> None:
    """証明書の検証の失敗・分類できない失敗・プロキシの拒否や認証の要求は、送り直しても解消しない失敗として分類する。"""
    assert is_resolvable_by_retry(failure) is False
